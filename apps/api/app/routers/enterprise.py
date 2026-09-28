from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, FastAPI
from sqlalchemy.engine import Connection, Engine

from boss_engine.config import Settings
from ..db import begin
from ..enterprise_extensions.soc2 import SOC2EvidenceService
from ..schemas import SOC2EvidenceRequest

from .route_ownership import ENTERPRISE_EDITIONS, is_enterprise_path

enterprise_router = APIRouter()


def register_soc2_route(
    app: FastAPI,
    engine: Engine,
    settings: Settings,
    require_project_actor: Callable[[Connection, str, str, str], None],
) -> None:
    """Register paid compliance collection before edition route partitioning."""

    service = SOC2EvidenceService(settings)

    @app.post("/v1/enterprise/soc2-evidence", status_code=201)
    def collect_soc2_evidence(payload: SOC2EvidenceRequest) -> dict:
        with begin(engine) as connection:
            require_project_actor(
                connection, payload.project_id, payload.actor, "audit_export"
            )
            return service.collect_evidence(
                connection, payload.project_id, actor=payload.actor
            )


__all__ = [
    "ENTERPRISE_EDITIONS",
    "enterprise_router",
    "is_enterprise_path",
    "register_soc2_route",
]
