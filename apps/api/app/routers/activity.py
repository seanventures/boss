"""Global searchable activity log HTTP — thin read over ActivityLogService.

GET /v1/activity is a projection of events + process steps + jobs +
deadletters + receipts. It does not invent a second event store.

GET /v1/events stays the unauthenticated SoR list; this route matches that
permission model (no require_project_actor). If ActivityLogService cannot be
imported, return an honest 503 and never fabricate items.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from ..config import get_settings
from ..db import begin, get_engine


router = APIRouter(tags=["activity"])

UNAVAILABLE = "Activity log service is unavailable."
ALLOWED_SORT = frozenset({"created_at", "status", "kind"})
ALLOWED_ORDER = frozenset({"desc", "asc"})
LIST_METHODS = ("list_activity", "list", "query")


def _load_service_class() -> Optional[type]:
    try:
        from boss_engine.services.activity_log import ActivityLogService
    except ImportError:
        return None
    return ActivityLogService


def _service() -> Any:
    cls = _load_service_class()
    if cls is None:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    return cls(get_settings())


def _resolve_project_id(project_id: Optional[str]) -> str:
    from apps.api.app.main import resolve_list_project_id

    return resolve_list_project_id(project_id)


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _invoke_list(service: Any, connection: Any, **kwargs: Any) -> Any:
    for name in LIST_METHODS:
        fn = getattr(service, name, None)
        if callable(fn):
            return fn(connection, **kwargs)
    raise HTTPException(status_code=503, detail=f"{UNAVAILABLE} Missing {LIST_METHODS[0]}.")


def _envelope(
    payload: Any, *, project_id: str, limit: int, offset: int
) -> dict[str, Any]:
    if isinstance(payload, list):
        items = payload
        extra: dict[str, Any] = {}
    elif isinstance(payload, dict) and "items" in payload:
        items = payload.get("items")
        extra = {key: value for key, value in payload.items() if key != "items"}
    else:
        raise HTTPException(
            status_code=503,
            detail=f"{UNAVAILABLE} Invalid list payload.",
        )
    if items is None:
        items = []
    if not isinstance(items, list):
        raise HTTPException(
            status_code=503,
            detail=f"{UNAVAILABLE} Invalid list payload.",
        )
    return {
        **extra,
        "project_id": extra.get("project_id") or project_id,
        "items": items,
        "limit": extra.get("limit", limit),
        "offset": extra.get("offset", offset),
    }


@router.get("/v1/activity")
def list_activity(
    project_id: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    actor: Optional[str] = Query(default=None),
    entity_type: Optional[str] = Query(default=None),
    # The facets an operator actually reasons in: how loud, which stream, which work task,
    # which AI agent. Everything above this line predates per-turn capture.
    level: Optional[str] = Query(default=None, description="verbose | info | warn | error"),
    source: Optional[str] = Query(
        default=None,
        description="event | process_step | job | deadletter | receipt | llm_call | "
                    "agent_turn | infra (jobs+deadletters+receipts together)",
    ),
    run_id: Optional[str] = Query(default=None, description="one work task (crun_...)"),
    agent: Optional[str] = Query(default=None, description="one AI agent, by role key"),
    since: Optional[str] = Query(default=None),
    until: Optional[str] = Query(default=None),
    sort: str = Query(default="created_at"),
    order: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Searchable, sortable ops timeline. Honest empty; never fake items."""
    project_id = _resolve_project_id(project_id)
    sort_key = (sort or "created_at").strip().lower()
    order_key = (order or "desc").strip().lower()
    if sort_key not in ALLOWED_SORT:
        raise HTTPException(
            status_code=422,
            detail="sort must be one of: created_at, status, kind",
        )
    if order_key not in ALLOWED_ORDER:
        raise HTTPException(status_code=422, detail="order must be one of: desc, asc")

    if not callable(_service):
        raise HTTPException(status_code=503, detail=UNAVAILABLE)

    with begin(get_engine()) as connection:
        try:
            service = _service()
            if service is None:
                raise HTTPException(status_code=503, detail=UNAVAILABLE)
            payload = _invoke_list(
                service,
                connection,
                project_id=project_id,
                q=_clean(q),
                kind=_clean(kind),
                status=_clean(status),
                actor=_clean(actor),
                entity_type=_clean(entity_type),
                level=_clean(level),
                source=_clean(source),
                run_id=_clean(run_id),
                agent=_clean(agent),
                since=_clean(since),
                until=_clean(until),
                sort=sort_key,
                order=order_key,
                limit=limit,
                offset=offset,
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _envelope(payload, project_id=project_id, limit=limit, offset=offset)


__all__ = ["router"]


@router.post("/v1/activity/retention/prune")
def prune_activity_retention(payload: Optional[dict] = None) -> dict[str, Any]:
    """Age log rows out of the retention window. Called hourly by the worker.

    The floor lives in the service, not here, so a caller cannot ask for a shorter window than
    an operator debugging a customer build needs. Deletes ROWS from the transcript table only —
    never files, and never the audit stream.
    """
    from boss_engine.services.activity_retention import prune, retention_days

    requested = None
    if isinstance(payload, dict) and payload.get("days") is not None:
        try:
            requested = int(payload["days"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="days must be an integer")

    with begin(get_engine()) as connection:
        result = prune(connection, days=requested)
    result["floor_days"] = retention_days()
    return result
