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
