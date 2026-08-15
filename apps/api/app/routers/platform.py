"""Platform Ops epic routers — identity, on-call, security, release, etc.

Agents add routers here and register them in ``mount_platform_routers``.
Keep domain logic in ``engine/boss_engine/services/*``; routes stay thin.
"""
from __future__ import annotations

from fastapi import FastAPI


def mount_platform_routers(app: FastAPI) -> None:
    """Include optional platform domain routers when present."""
    # Identity IdP catalog + setup blueprints
    try:
        from .identity_providers import router as identity_router

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
