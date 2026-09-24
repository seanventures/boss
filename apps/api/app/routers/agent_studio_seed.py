"""Isolated Agent Studio catalog seed (BOSS-347 / W4.3).

Installs the reviewed SpecPack starter catalog. Definitions only: no jobs,
no budget reservation, no model calls, no live agent launch.

Fail-closed 503 when studio tables are missing. Never 500, never silent success.
Also mounts ``POST /v1/agent-studio/seed`` so the shorter path is not a 404.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.services.agent_studio import (
    AgentStudioSchemaMissingError,
    AgentStudioService,
)
from boss_engine.services.receipts import ReceiptService


router = APIRouter(tags=["agent-studio-seed"])

SCHEMA_MISSING_DETAIL = (
    "Agent Studio schema is not applied on this runtime. "
    "Apply db/schema.sql (agent_definitions / agent_versions / "
    "panel_definitions / panel_versions) before seeding."
)

SEED_DEDUPE_PREFIX = "agent-studio:catalog-seed"


class SeedCatalogRequest(BaseModel):
    project_id: str
    actor: str = "vp_agents"


@router.get("/v1/agent-studio/templates")
def list_agent_templates(project_id: str) -> dict[str, Any]:
    """Catalog cards for Org Chart; installation is separate from execution."""
    service = AgentStudioService(get_settings())
    with begin(get_engine()) as connection:
        _project_scope(connection, project_id)
        ready, missing = service._catalog_tables_ready(connection)
        if not ready:
            raise HTTPException(
                status_code=503,
                detail="Agent Studio schema is not applied: " + ", ".join(missing),
            )
        installed = {
            item["agent_key"]: item
            for item in service.list_agents(connection, project_id)
        }
        return {
            "project_id": project_id,
            "items": [
                {
                    **item,
                    "installed": item["id"] in installed,
                    "active": bool(installed.get(item["id"], {}).get("active_version_id")),
                }
                for item in service.list_templates()
            ],
        }


@router.post("/v1/agent-studio/templates/{template_id}/install")
def install_agent_template(
    template_id: str, payload: SeedCatalogRequest
) -> dict[str, Any]:
    """Install one project-scoped definition with the existing Studio gates."""
    settings = get_settings()
    service = AgentStudioService(settings)
    receipts = ReceiptService()
    with begin(get_engine()) as connection:
        from apps.api.app.main import require_project_actor

        require_project_actor(
            connection, payload.project_id, payload.actor, "role_review"
        )
        scope = _project_scope(connection, payload.project_id)
        template = next(
            (item for item in service.list_templates() if item["id"] == template_id),
            None,
        )
        if template is None:
            raise HTTPException(status_code=404, detail="Agent template not found.")
        dedupe_key = f"agent-studio:template-install:{payload.project_id}:{template_id}"
        receipt, _created = receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            payload.project_id,
            dedupe_key,
            "agent_studio.template_install",
            {"project_id": payload.project_id, "template_id": template_id},
            {"project_id": payload.project_id, "actor": payload.actor,
             "external_effects": 0, "model_calls": 0, "jobs_enqueued": 0},
        )
        try:
            seeded = service.seed_template(
                connection, payload.project_id, template_id, payload.actor
            )
        except (AgentStudioSchemaMissingError, ProgrammingError, ValueError) as error:
            raise _schema_http_error(error) from error
        receipts.mark_succeeded(
            connection,
            receipt["id"],
            None,
            {"agent_key": seeded["agent_key"], "status": "installed",
             "external_effects": 0, "model_calls": 0, "jobs_enqueued": 0},
        )
        return {
            **seeded,
            "status": "installed",
            "receipt_id": receipt["id"],
            "dedupe_key": dedupe_key,
            "external_effects": 0,
            "model_calls": 0,
            "jobs_enqueued": 0,
            "launched_live_agent": False,
        }


def _project_scope(connection: Any, project_id: str) -> dict[str, str]:
    row = connection.execute(
        text(
            """
            SELECT w.org_id, p.workspace_id
            FROM projects p
            JOIN workspaces w ON w.id = p.workspace_id
            WHERE p.id = :project_id
            """
        ),
        {"project_id": project_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=422, detail="project not found")
    return {"org_id": row["org_id"], "workspace_id": row["workspace_id"]}


def _schema_http_error(error: Exception) -> HTTPException:
    if isinstance(error, AgentStudioSchemaMissingError):
        return HTTPException(status_code=503, detail=str(error))
    orig = getattr(error, "orig", None)
    name = type(orig).__name__ if orig is not None else type(error).__name__
    blob = str(orig or error).lower()
    if name == "UndefinedTable" or "does not exist" in blob:
        return HTTPException(status_code=503, detail=SCHEMA_MISSING_DETAIL)
    return HTTPException(status_code=422, detail=str(error))


def _seed_catalog(payload: SeedCatalogRequest) -> dict[str, Any]:
    """Idempotent project-scoped catalog install. Zero spend, zero dispatch."""
    settings = get_settings()
    service = AgentStudioService(settings)
    receipts = ReceiptService()
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        from apps.api.app.main import require_project_actor

        try:
            require_project_actor(
                connection, payload.project_id, payload.actor, "role_review"
            )
        except ProgrammingError as error:
            raise _schema_http_error(error) from error

        ready, missing = service._catalog_tables_ready(connection)
        if not ready:
            raise HTTPException(
                status_code=503,
                detail="Agent Studio schema is not applied: " + ", ".join(missing),
            )

        scope = _project_scope(connection, payload.project_id)
        dedupe_key = f"{SEED_DEDUPE_PREFIX}:{payload.project_id}"
        receipt, _created = receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            payload.project_id,
            dedupe_key,
            "agent_studio.catalog_seed",
            {"project_id": payload.project_id, "kind": "starter_catalog"},
            {
                "project_id": payload.project_id,
                "actor": payload.actor,
                "external_effects": 0,
                "model_calls": 0,
                "jobs_enqueued": 0,
            },
        )
        try:
            seeded = service.seed_catalog(
                connection,
                payload.project_id,
                actor=payload.actor,
            )
            from boss_engine.services.workflows import seed_specpack_workflows

            try:
                workflows = seed_specpack_workflows(
                    connection,
                    project_id=payload.project_id,
                    workflow_root=settings.specpack_root / "workflows",
                    actor=payload.actor,
                )
            except ProgrammingError as error:
                workflows = []
                skipped = list(seeded.get("skipped") or [])
                skipped.append(
                    {"path": "workflows/*.yaml", "error": str(error)}
                )
                seeded["skipped"] = skipped
        except (AgentStudioSchemaMissingError, ProgrammingError) as error:
            receipts.mark_failed(
                connection,
                receipt["id"],
                {"error": str(error), "fail_closed": True},
            )
            raise _schema_http_error(error) from error
        except Exception as error:  # noqa: BLE001 - typed API mapping
            receipts.mark_failed(connection, receipt["id"], {"error": str(error)})
            raise _schema_http_error(error) from error

        result = {
            "project_id": payload.project_id,
            "status": "seeded",
            "agents": seeded["agents"],
            "panels": seeded["panels"],
            "workflows": workflows,
            "skipped": seeded.get("skipped") or [],
            "agent_count": len(seeded["agents"]),
            "panel_count": len(seeded["panels"]),
            "workflow_count": len(workflows),
            "skipped_count": len(seeded.get("skipped") or []),
            "external_effects": 0,
            "model_calls": 0,
            "jobs_enqueued": 0,
            "budget_reserved": False,
            "launched_live_agents": False,
            "receipt_id": receipt["id"],
            "dedupe_key": dedupe_key,
        }
        receipts.mark_succeeded(
            connection,
            receipt["id"],
            None,
            {
                "agent_count": result["agent_count"],
                "panel_count": result["panel_count"],
                "workflow_count": result["workflow_count"],
                "status": "seeded",
                "external_effects": 0,
                "model_calls": 0,
                "jobs_enqueued": 0,
            },
        )
        return result


@router.post("/v1/agent-studio/catalog/seed")
def seed_agent_studio_catalog(payload: SeedCatalogRequest) -> dict[str, Any]:
    """Canonical starter-catalog install. Definitions only; no live launch."""
    return _seed_catalog(payload)


@router.post("/v1/agent-studio/seed")
def seed_agent_studio_catalog_alias(payload: SeedCatalogRequest) -> dict[str, Any]:
    """Alias for operators who hit ``/v1/agent-studio/seed`` instead of catalog/seed."""
    return _seed_catalog(payload)
