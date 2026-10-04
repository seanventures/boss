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


def _invoke_list(log_service: Any, connection: Any, /, **kwargs: Any) -> Any:
    """POSITIONAL-ONLY, deliberately. Every remaining argument is a caller-supplied FILTER
    forwarded by name, so any parameter here shares a namespace with the query string. Adding
    a `service=api|worker` filter collided with a parameter that happened to be called
    `service` and raised "got multiple values" on every request. The `/` makes that structural
    rather than a naming convention to remember."""
    for name in LIST_METHODS:
        fn = getattr(log_service, name, None)
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
        description="Which streams to include. One name, a group, or a comma-separated set "
                    "of either — e.g. 'workflow_step,deployment'. Omit for every stream. "
                    "Names: event | process_step | job | deadletter | receipt | llm_call | "
                    "agent_turn | app_log | workflow_step | deployment. "
                    "Groups: queue (jobs+deadletters+receipts; 'infra' is the historical "
                    "spelling) | platform (deployments + app/container output).",
    ),
    run_id: Optional[str] = Query(default=None, description="one work task (crun_...)"),
    workflow_run_id: Optional[str] = Query(
        default=None,
        description="one workflow run (wfr_...). Not the same scope as run_id: a workflow "
                    "run is the governed run a coding task was one step of.",
    ),
    agent: Optional[str] = Query(default=None, description="one AI agent, by role key"),
    service: Optional[str] = Query(
        default=None,
        description="which BOSS process said it: api | worker. Rows that do not record a "
                    "process are excluded rather than guessed at.",
    ),
    step_id: Optional[str] = Query(
        default=None,
        description="one step of the workflow run named by workflow_run_id: its own rows and "
                    "the work task it started.",
    ),
    since: Optional[str] = Query(default=None),
    until: Optional[str] = Query(default=None),
    sort: str = Query(default="created_at"),
    order: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    cursor: Optional[str] = Query(
        default=None,
        description="next_cursor from the previous page: the rows strictly older than it.",
    ),
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
            # NOT `service` — that is the name of the `service=api|worker` query filter, and
            # binding the ActivityLogService to it overwrote the caller's value, so every
            # request forwarded a service OBJECT where a string was expected.
            log_service = _service()
            if log_service is None:
                raise HTTPException(status_code=503, detail=UNAVAILABLE)
            payload = _invoke_list(
                log_service,
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
                workflow_run_id=_clean(workflow_run_id),
                agent=_clean(agent),
                service=_clean(service),
                step_id=_clean(step_id),
                since=_clean(since),
                until=_clean(until),
                sort=sort_key,
                order=order_key,
                limit=limit,
                offset=offset,
                cursor=_clean(cursor),
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _envelope(payload, project_id=project_id, limit=limit, offset=offset)


@router.get("/v1/activity/facets")
def activity_facets(
    project_id: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None),
    until: Optional[str] = Query(default=None),
    run_id: Optional[str] = Query(default=None),
    workflow_run_id: Optional[str] = Query(default=None),
    agent: Optional[str] = Query(default=None),
    service: Optional[str] = Query(default=None),
    step_id: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    """How many rows the log holds per level and per stream, counted against the log.

    The console's chips used to count the 200 rows on screen and read as totals (boss-1:
    ERROR 0 and Deadletters 0 beside 14 dead-letters in Operations).
    """
    project_id = _resolve_project_id(project_id)
    with begin(get_engine()) as connection:
        log_service = _service()
        facets = getattr(log_service, "facets", None)
        if not callable(facets):
            raise HTTPException(status_code=503, detail=f"{UNAVAILABLE} Missing facets.")
        try:
            return facets(
                connection,
                project_id,
                since=_clean(since),
                until=_clean(until),
                run_id=_clean(run_id),
                workflow_run_id=_clean(workflow_run_id),
                agent=_clean(agent),
                service=_clean(service),
                step_id=_clean(step_id),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


__all__ = ["router"]


@router.post("/v1/activity/retention/prune")
def prune_activity_retention(payload: Optional[dict] = None) -> dict[str, Any]:
    """Age log rows out of the retention window. Called hourly by the worker.

    The floor lives in the service, not here, so a caller cannot ask for a shorter window than
    an operator debugging a customer build needs. Deletes ROWS from the transcript table only —
    never files, and never the audit stream.
    """
    from apps.api.app.main import require_worker_principal
    from boss_engine.services.activity_retention import prune, retention_days

    require_worker_principal()
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
