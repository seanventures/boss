"""Platform Ops epic routers — identity, on-call, security, release, etc.

Agents add routers here and register them in ``mount_platform_routers``.
Keep domain logic in ``engine/boss_engine/services/*``; routes stay thin.
"""
from __future__ import annotations

from fastapi import FastAPI


def mount_platform_routers(app: FastAPI) -> None:
    """Include optional platform domain routers when present."""
    # Workflow test runs with pinned data, and re-running a run from a step (#512). Imported
    # unguarded: a test run's safety rules live behind it, and a route that silently vanished
    # on an ImportError would leave the designer's Test run button pointing at nothing.
    from .workflow_test_runs import router as workflow_test_runs_router

    app.include_router(workflow_test_runs_router)
    # System health: deploy readiness, the system doctor's findings and actions (stability).
    try:
        from .system_health import router as system_health_router

        app.include_router(system_health_router)
    except ImportError:
        pass
    # Identity IdP catalog + setup blueprints
    try:
        from .idp_catalog import router as identity_router

        app.include_router(identity_router)
    except ImportError:
        pass

    # On-call schedules, escalations, SRE automation
    try:
        from .oncall_sre import router as oncall_router

        app.include_router(oncall_router)
    except ImportError:
        pass

    # AI SRE capability ledger (awesome-ai-sre map)
    try:
        from .ai_sre import router as ai_sre_router

        app.include_router(ai_sre_router)
    except ImportError:
        pass

    # AI SRE observe-only overwatch tick (do not fold into ai_sre.py)
    try:
        from .ai_sre_overwatch import router as ai_sre_overwatch_router

        app.include_router(ai_sre_overwatch_router)
    except ImportError:
        pass

    # First-class incident runbooks (validate + Decision; not GA)
    try:
        from .sre_runbooks import router as sre_runbooks_router

        app.include_router(sre_runbooks_router)
    except ImportError:
        pass

    # AIOps noise report + NL observability query plan (not live PromQL)
    try:
        from .sre_noise import router as sre_noise_router

        app.include_router(sre_noise_router)
    except ImportError:
        pass

    # Evidence-only SRE incident postmortems (not break-glass, not GA)
    try:
        from .sre_postmortem import router as sre_postmortem_router

        app.include_router(sre_postmortem_router)
    except ImportError:
        pass

    # Incident response: an app's own signals door, the incident card, the report export.
    # Imported unguarded: a door that silently fails to mount is an app that cannot report.
    from .incident_response import router as incident_response_router

    app.include_router(incident_response_router)

    # Deterministic SRE bench (no vendors)
    try:
        from .sre_bench import router as sre_bench_router

        app.include_router(sre_bench_router)
    except ImportError:
        pass

    # Security vulnerability scanning (Semgrep/Trivy/OSV)
    try:
        from .security_scan import router as security_router

        app.include_router(security_router)
    except ImportError:
        pass

    # Project release planning + status board
    try:
        from .release_planning import router as release_router

        app.include_router(release_router)
    except ImportError:
        pass

    # Community v1 lock summary (in / out / cloud counts only)
    try:
        from .v1_lock import router as v1_lock_router

        app.include_router(v1_lock_router)
    except ImportError:
        pass

    # Token efficiency / packing optimization
    try:
        from .token_efficiency import router as token_router

        app.include_router(token_router)
    except ImportError:
        pass

    # Distributed worker autoscaling control plane
    try:
        from .worker_autoscaling import router as worker_as_router

        app.include_router(worker_as_router)
    except ImportError:
        pass

    # Observability suite expansions
    try:
        from .observability_suite import router as obs_router

        app.include_router(obs_router)
    except ImportError:
        pass

    # CI/CD pipeline provenance & multi-vendor generate
    try:
        from .cicd_pipelines import router as cicd_router

        app.include_router(cicd_router)
    except ImportError:
        pass

    try:
        from .provider_conformance import router as provider_conf_router
        app.include_router(provider_conf_router)
    except ImportError:
        pass

    # Concurrency & scheduling governance (BOSS-100)
    try:
        from .concurrency import router as concurrency_router

        app.include_router(concurrency_router)
    except ImportError:
        pass

    # Safety gates: branch protection / live-demo ready (BOSS-324)
    try:
        from .safety_gates import router as safety_router

        app.include_router(safety_router)
    except ImportError:
        pass

    # Native Work Plane — initiatives / modules / cycles / roadmaps (BOSS-331/358)
    try:
        from .work_plane import router as work_plane_router

        app.include_router(work_plane_router)
    except ImportError:
        pass

    # BOSSAPP-622 — knowledge graph read and manage surface
    try:
        from .knowledge import router as knowledge_router

        app.include_router(knowledge_router)
    except ImportError:
        pass
    # BOSSAPP-768 — the Graph area's read path (overview, neighbourhood, node, tree, search)
    try:
        from .knowledge_view import router as knowledge_view_router

        app.include_router(knowledge_view_router)
    except ImportError:
        pass

    # BOSSAPP-768 S5 — archive / restore a project. Unguarded: a broken import fails the boot.
    from .projects_archive import router as projects_archive_router

    app.include_router(projects_archive_router)

    # Delete a project with a full cleanup (services/projects_delete.py).
    from .projects_delete import router as projects_delete_router

    app.include_router(projects_delete_router)

    # BOSSAPP-810 — New project: the project page's view and Import's repository facts (reads).
    from .start_project import router as start_project_router

    app.include_router(start_project_router)

    # BOSSAPP-913/914 — the Project experience: one read per Project-page tab, the captain, the
    # onboarding agent, ⌘K and Project › Settings. Unguarded: a broken import fails the boot.
    from .project_experience import router as project_experience_router

    app.include_router(project_experience_router)
    # Add a project › Folder on host (screen 03): read-only folder probe + the source record.
    from .host_folders import router as host_folders_router

    app.include_router(host_folders_router)
    # Local-first projects (BOSSAPP-915): the local repository, its change sets and Merge,
    # import, the machine checks, Publish to GitHub, and work waiting for GitHub.
    from .local_projects import router as local_projects_router

    app.include_router(local_projects_router)
    # The change page as a git-style review, local and GitHub alike (BOSSAPP-954).
    from .change_review import router as change_review_router

    app.include_router(change_review_router)
    # The local git remote (BOSSAPP-948): /git/<project>.git over git's smart HTTP.
    from .local_git import router as local_git_router

    app.include_router(local_git_router)
    # Infra › Add a server (agentic-factory 3): read + probe, Add and set up, the setup page.
    from .add_server import router as add_server_router

    app.include_router(add_server_router)
    # Project › Health › Watch this app (agentic-factory 2): the SRE agent's read, Start
    # watching, the landing and its one gate — over Observability's rules and contact points.
    from .watch_app import router as watch_app_router

    app.include_router(watch_app_router)
    # Add a project v2 (new-project-v2): Helm's understanding, the preview, Create and start.
    from .add_project import router as add_project_router

    app.include_router(add_project_router)

    # BOSSAPP-768 — Rules review: the business rules agents propose, approved by a person
    try:
        from .knowledge_rules import router as knowledge_rules_router

        app.include_router(knowledge_rules_router)
    except ImportError:
        pass

    # Agent design review and lock, per workflow stage (design §4.6)
    try:
        from .workflow_designs import router as workflow_designs_router

        app.include_router(workflow_designs_router)
    except ImportError:
        pass

    # KG P7a — a regulation document and its project's ontology pack
    try:
        from .knowledge_documents import router as knowledge_documents_router

        app.include_router(knowledge_documents_router)
    except ImportError:
        pass

    # BOSSAPP-768 — a knowledge-graph project's guided setup (import, team, first scan, review)
    try:
        from .knowledge_setup import router as knowledge_setup_router

        app.include_router(knowledge_setup_router)
    except ImportError:
        pass

    # BOSSAPP-768 — learning while executing: the live feed and the before/after proof
    try:
        from .knowledge_learning import router as knowledge_learning_router

        app.include_router(knowledge_learning_router)
    except ImportError:
        pass

    # BOSS-348 — earned autonomy score + value-based schedule preference
    try:
        from .earned_autonomy import router as earned_autonomy_router

        app.include_router(earned_autonomy_router)
    except ImportError:
        pass

    # BOSS-11 — persistent preview sandboxes (Workload B)
    try:
        from .preview_sandboxes import router as preview_sandbox_router

        app.include_router(preview_sandbox_router)
    except ImportError:
        pass

    # Agent-wide OSS key management (KMS)
    try:
        from .agent_kms import router as agent_kms_router

        app.include_router(agent_kms_router)
    except ImportError:
        pass

    # Community meetings simulated E2E + evidence
    try:
        from .meetings_e2e import router as meetings_e2e_router

        app.include_router(meetings_e2e_router)
    except ImportError:
        pass

    # Community assurance demo seed (W4.5)
    try:
        from .assurance_seed import router as assurance_seed_router

        app.include_router(assurance_seed_router)
    except ImportError:
        pass

    # Local OpenAI-compatible entitlement source
    try:
        from .local_model_source import router as local_model_router

        app.include_router(local_model_router)
    except ImportError:
        pass

    # Community BYOK paste-and-connect (local_vault write-through)
    try:
        from .provider_easy_connect import router as provider_easy_connect_router

        app.include_router(provider_easy_connect_router)
    except ImportError:
        pass

    # No-spend preview fixture (View HTML without microVM)
    try:
        from .preview_fixture import router as preview_fixture_router

        app.include_router(preview_fixture_router)
    except ImportError:
        pass

    # Freeze-safe Firecracker echo exercise
    try:
        from .sandbox_echo import router as sandbox_echo_router

        app.include_router(sandbox_echo_router)
    except ImportError:
        pass

    # Community comms dry-run close-the-loop (no Slack/email send)
    try:
        from .comms_dry_run import router as comms_dry_run_router

        app.include_router(comms_dry_run_router)
    except ImportError:
        pass

    # DLQ operator remediations (list/create/retry) — do not add these to main.py
    try:
        from .deadletter_ops import router as deadletter_ops_router

        app.include_router(deadletter_ops_router)
    except ImportError:
        pass

    # Community go/no-go meeting fixture (no LLM)
    try:
        from .meetings_go_no_go import router as meetings_go_no_go_router

        app.include_router(meetings_go_no_go_router)
    except ImportError:
        pass

    # Agent Studio catalog seed (no spend / no live launch)
    try:
        from .agent_studio_seed import router as agent_studio_seed_router

        app.include_router(agent_studio_seed_router)
    except ImportError:
        pass

    # Metatron captain: persistent session, goals, watch, validated drive, overwatch
    try:
        from .metatron_captain import router as metatron_captain_router

        app.include_router(metatron_captain_router)
    except ImportError:
        pass

    try:
        from .captain import router as captain_router

        app.include_router(captain_router)
    except ImportError:
        pass

    # Focused always-on agents: builder + validated duty loop
    try:
        from .focused_agents import router as focused_agents_router

        app.include_router(focused_agents_router)
    except ImportError:
        pass

    # Agent process / handoff workflows (PRD → review → build → certify → accept)
    try:
        from .agent_processes import router as agent_processes_router

        app.include_router(agent_processes_router)
    except ImportError:
        pass

    # Project Manager steward: operate / test / validate / propose
    try:
        from .process_manager import router as process_manager_router

        app.include_router(process_manager_router)
    except ImportError:
        pass

    # Global searchable activity log (BOSS-379)
    try:
        from .activity import router as activity_router

        app.include_router(activity_router)
    except ImportError:
        pass

    # Govern: the Limits board and its tabs (one read each; writes stay on their own routes).
    from .govern import router as govern_router

    app.include_router(govern_router)

    # Govern › Where people decide: the ONE compliance list (services/decide_list.py).
    # Imported unguarded: the list is the boundary every gate reads; a broken import fails boot.
    from .decide_list import router as decide_list_router

    app.include_router(decide_list_router)

    try:
        from .factory_flow import router as factory_flow_router

        app.include_router(factory_flow_router)
    except ImportError:
        pass

    # Webhook triggers for workflows (#512): owner routes and the signed public door.
    # Imported unguarded: a broken import must fail the boot, not quietly drop the door.
    from .workflow_webhooks import router as workflow_webhooks_router

    app.include_router(workflow_webhooks_router)
