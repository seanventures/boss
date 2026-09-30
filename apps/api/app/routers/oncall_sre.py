"""HTTP surface for BOSS-native on-call / SRE automation.

Mounted at ``/v1/oncall-sre`` (distinct from RB-20 ``/v1/oncall`` notify cards).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.services.oncall_sre import (
    ADAPTERS,
    OncallSreError,
    OncallSreService,
)

from ..config import get_settings
from ..db import begin, get_engine

router = APIRouter(prefix="/v1/oncall-sre", tags=["oncall-sre"])
_service: Optional[OncallSreService] = None


def _svc() -> OncallSreService:
    global _service
    if _service is None:
        _service = OncallSreService(get_settings())
    return _service


def _engine():
    return get_engine()


def _require_actor(connection, project_id: str, actor: str, permission: str = "providerops_manage") -> None:
    # Deferred import avoids circular load with main during platform mount.
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, permission)


def _raise(err: OncallSreError) -> None:
    detail: Any = {"message": str(err), **(err.payload or {})}
    raise HTTPException(status_code=err.status_code, detail=detail)


# ── request models ───────────────────────────────────────────────────────────
class ActorBody(BaseModel):
    project_id: str
    actor: str = "executive_console"


class ScheduleCreate(ActorBody):
    name: str
    timezone: str = "UTC"
    layers: list[dict[str, Any]] = Field(default_factory=list)
    enabled: bool = True


class PolicyCreate(ActorBody):
    name: str
    schedule_id: Optional[str] = None
    steps: list[dict[str, Any]] = Field(default_factory=list)


class PageCreate(ActorBody):
    title: str = ""
    severity: str = "warning"
    service: str = ""
    reliability_incident_id: Optional[str] = None
    schedule_id: Optional[str] = None
    policy_id: Optional[str] = None
    adapter: str = "slack_webhook"
    dedupe_key: Optional[str] = None
    force_live: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)


class PageAction(ActorBody):
    note: str = ""


class FromReliability(ActorBody):
    limit: int = Field(default=20, ge=1, le=100)
    policy_id: Optional[str] = None
    schedule_id: Optional[str] = None
    adapter: str = "slack_webhook"
    force_live: bool = False


class GraphLink(ActorBody):
    page_id: Optional[str] = None
    reliability_incident_id: Optional[str] = None
    node_kind: str = "service"
    node_key: str
    edge_kind: str = "correlates_with"


# ── schedules ────────────────────────────────────────────────────────────────
@router.get("/schedules")
def list_schedules(project_id: str = Query(...)) -> dict[str, Any]:
    with begin(_engine()) as connection:
        items = _svc().list_schedules(connection, project_id)
    return {"project_id": project_id, "items": items}


@router.post("/schedules", status_code=201)
def create_schedule(payload: ScheduleCreate) -> dict[str, Any]:
    with begin(_engine()) as connection:
        _require_actor(connection, payload.project_id, payload.actor)
        try:
            row = _svc().create_schedule(
                connection,
                payload.project_id,
                payload.name,
                timezone=payload.timezone,
                layers=payload.layers,
                enabled=payload.enabled,
                actor=payload.actor,
            )
        except OncallSreError as err:
            _raise(err)
    return row


@router.get("/schedules/{schedule_id}")
def get_schedule(schedule_id: str, project_id: str = Query(...)) -> dict[str, Any]:
    with begin(_engine()) as connection:
        row = _svc().get_schedule(connection, project_id, schedule_id)
    if not row:
        raise HTTPException(status_code=404, detail="schedule not found")
    return row


# ── policies ─────────────────────────────────────────────────────────────────
@router.get("/policies")
def list_policies(project_id: str = Query(...)) -> dict[str, Any]:
    with begin(_engine()) as connection:
        items = _svc().list_policies(connection, project_id)
    return {"project_id": project_id, "items": items}


@router.post("/policies", status_code=201)
def create_policy(payload: PolicyCreate) -> dict[str, Any]:
    with begin(_engine()) as connection:
        _require_actor(connection, payload.project_id, payload.actor)
        try:
            row = _svc().create_policy(
                connection,
                payload.project_id,
                payload.name,
                steps=payload.steps,
                schedule_id=payload.schedule_id,
                actor=payload.actor,
            )
        except OncallSreError as err:
            _raise(err)
    return row


@router.get("/policies/{policy_id}")
def get_policy(policy_id: str, project_id: str = Query(...)) -> dict[str, Any]:
    with begin(_engine()) as connection:
        row = _svc().get_policy(connection, project_id, policy_id)
    if not row:
        raise HTTPException(status_code=404, detail="policy not found")
    return row


# ── pages / incidents ────────────────────────────────────────────────────────
@router.get("/pages")
def list_pages(
    project_id: str = Query(...),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    with begin(_engine()) as connection:
        items = _svc().list_pages(connection, project_id, status=status, limit=limit)
    return {"project_id": project_id, "items": items}


@router.post("/incidents/{incident_id}/page")
def page_incident(incident_id: str, payload: PageCreate) -> dict[str, Any]:
    """Page from a reliability incident id, or open a synthetic page for that id."""
    with begin(_engine()) as connection:
        _require_actor(connection, payload.project_id, payload.actor)
        try:
            if payload.adapter not in ADAPTERS:
                raise OncallSreError(
                    f"unknown adapter {payload.adapter!r}",
                    status_code=400,
                )
            result = _svc().page_reliability_incident(
                connection,
                payload.project_id,
                incident_id,
                title=payload.title,
                severity=payload.severity,
                service=payload.service,
                schedule_id=payload.schedule_id,
                policy_id=payload.policy_id,
                adapter=payload.adapter,
                dedupe_key=payload.dedupe_key,
                force_live=payload.force_live,
                payload=payload.payload,
                actor=payload.actor,
            )
        except OncallSreError as err:
            _raise(err)
    return result


@router.post("/pages")
def open_page(payload: PageCreate) -> dict[str, Any]:
    """Open a synthetic or reliability-linked page without path incident id."""
    with begin(_engine()) as connection:
        _require_actor(connection, payload.project_id, payload.actor)
        try:
            result = _svc().page(
                connection,
                payload.project_id,
                title=payload.title or "On-call page",
                severity=payload.severity,
                service=payload.service,
                reliability_incident_id=payload.reliability_incident_id,
                schedule_id=payload.schedule_id,
                policy_id=payload.policy_id,
                adapter=payload.adapter,
                dedupe_key=payload.dedupe_key,
                force_live=payload.force_live,
                payload=payload.payload,
                actor=payload.actor,
            )
        except OncallSreError as err:
            _raise(err)
    return result


@router.post("/incidents/{page_id}/ack")
def ack_page(page_id: str, payload: PageAction) -> dict[str, Any]:
    with begin(_engine()) as connection:
        _require_actor(connection, payload.project_id, payload.actor)
        try:
            result = _svc().ack(
                connection, payload.project_id, page_id,
                actor=payload.actor, note=payload.note,
            )
        except OncallSreError as err:
            _raise(err)
    return result


@router.post("/incidents/{page_id}/resolve")
def resolve_page(page_id: str, payload: PageAction) -> dict[str, Any]:
    with begin(_engine()) as connection:
        _require_actor(connection, payload.project_id, payload.actor)
        try:
            result = _svc().resolve(
                connection, payload.project_id, page_id,
                actor=payload.actor, note=payload.note,
            )
        except OncallSreError as err:
            _raise(err)
    return result


@router.get("/status")
def oncall_status(project_id: str = Query(...)) -> dict[str, Any]:
    with begin(_engine()) as connection:
        return _svc().status(connection, project_id)


@router.post("/from-reliability")
def from_reliability(payload: FromReliability) -> dict[str, Any]:
    with begin(_engine()) as connection:
        _require_actor(connection, payload.project_id, payload.actor)
        try:
            return _svc().from_reliability(
                connection,
                payload.project_id,
                limit=payload.limit,
                policy_id=payload.policy_id,
                schedule_id=payload.schedule_id,
                adapter=payload.adapter,
                force_live=payload.force_live,
                actor=payload.actor,
            )
        except OncallSreError as err:
            _raise(err)


@router.post("/graph/link")
def graph_link(payload: GraphLink) -> dict[str, Any]:
    with begin(_engine()) as connection:
        _require_actor(connection, payload.project_id, payload.actor)
        try:
            return _svc().graph_link(
                connection,
                payload.project_id,
                page_id=payload.page_id,
                reliability_incident_id=payload.reliability_incident_id,
                node_kind=payload.node_kind,
                node_key=payload.node_key,
                edge_kind=payload.edge_kind,
                actor=payload.actor,
            )
        except OncallSreError as err:
            _raise(err)


@router.get("/adapters")
def list_adapters() -> dict[str, Any]:
    from boss_engine.services.oncall_sre import adapter_credential

    items = []
    for a in ADAPTERS:
        live, env_name, _ = adapter_credential(a)
        items.append({"adapter": a, "live_capable": live, "token_env": env_name})
    return {"items": items}


__all__ = ["router"]
