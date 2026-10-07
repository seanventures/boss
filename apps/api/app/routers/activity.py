"""The activity log over HTTP — thin reads over ActivityLogService.

GET /v1/activity             one page, newest first, by keyset (cursor older, after newer)
GET /v1/activity/histogram   rows per time bucket, stacked by level (the pulse)
GET /v1/activity/facets      counts per level, stream, agent, run, process and status
GET /v1/activity/row/{id}    one line, with its labels
GET /v1/activity/context     the lines around one line, same run
GET /v1/activity/export      the current filter as CSV or NDJSON, bounded

Every read requires a reader of the project (it used to require nothing). Every read carries a
strong ETag built from the filters and the newest row of each stream, so a live tail that has
nothing new costs one index probe per stream and answers 304.
"""

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
import csv
import io
import json
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..db import begin


router = APIRouter(tags=["activity"])

UNAVAILABLE = "Activity log service is unavailable."


def _service() -> Any:
    try:
        from boss_engine.services.activity_log import ActivityLogService
    except ImportError as error:
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from error
    return ActivityLogService


def _service_class() -> Any:
    """The service, or an honest 503 — never fabricated items."""
    factory = globals().get("_service")
    if not callable(factory):
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    return factory()


def get_engine() -> Any:
    """The API's one engine and pool. `db.get_engine()` builds a NEW engine per call — a new
    pool and a new connection on every request — which alone costs more than a 304 may."""
    from apps.api.app.main import engine

    return engine


def _resolve_project_id(project_id: Optional[str]) -> str:
    from apps.api.app.main import resolve_list_project_id

    return resolve_list_project_id(project_id)


def _require_reader(connection: Any, project_id: str) -> None:
    from apps.api.app.main import require_project_reader

    require_project_reader(connection, project_id)


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _filters(
    *, q, kind, status, actor, entity_type, level, exclude_level, hide_verbose, source,
    exclude_source, run_id, workflow_run_id, agent, exclude_agent, service, step_id, act,
    subject, since, until, range_,
) -> dict[str, Any]:
    return {
        "q": _clean(q), "kind": _clean(kind), "status": _clean(status), "actor": _clean(actor),
        "entity_type": _clean(entity_type), "level": _clean(level),
        "exclude_level": _clean(exclude_level), "hide_verbose": _clean(hide_verbose),
        "source": _clean(source), "exclude_source": _clean(exclude_source),
        "run_id": _clean(run_id), "workflow_run_id": _clean(workflow_run_id),
        "agent": _clean(agent), "exclude_agent": _clean(exclude_agent),
        "service": _clean(service), "step_id": _clean(step_id), "act": _clean(act),
        "subject": _clean(subject), "since": _clean(since), "until": _clean(until),
        "range": _clean(range_),
    }


class _FilterParams:
    """The filters every read shares, declared once (FastAPI dependency)."""

    def __init__(
        self,
        q: Optional[str] = Query(default=None, description="free text (full-text where indexed)"),
        kind: Optional[str] = Query(default=None, description="one kind or a comma set; a "
                                    "stream name means that stream"),
        status: Optional[str] = Query(default=None, description="ok|failed|waiting|stuck|info"),
        actor: Optional[str] = Query(default=None),
        entity_type: Optional[str] = Query(default=None),
        level: Optional[str] = Query(default=None, description="one level or a comma set: "
                                     "verbose | info | warn | error"),
        exclude_level: Optional[str] = Query(default=None),
        hide_verbose: Optional[str] = Query(default=None, description="1 to leave verbose out "
                                            "when no level is chosen"),
        source: Optional[str] = Query(
            default=None,
            description="Which streams. One name, a group, or a comma set of either. Names: "
                        "event | process_step | job | deadletter | receipt | llm_call | "
                        "agent_turn | app_log | workflow_step | deployment. Groups: queue "
                        "('infra' is its historical spelling) | platform."),
        exclude_source: Optional[str] = Query(default=None),
        run_id: Optional[str] = Query(default=None, description="one work task (crun_...)"),
        workflow_run_id: Optional[str] = Query(default=None, description="one workflow run"),
        agent: Optional[str] = Query(default=None, description="role key, or a comma set"),
        exclude_agent: Optional[str] = Query(default=None),
        service: Optional[str] = Query(default=None, description="api | worker, or an app"),
        step_id: Optional[str] = Query(default=None, description="one step of workflow_run_id"),
        act: Optional[str] = Query(default=None, description="one piece of agent work (act_...)"),
        subject: Optional[str] = Query(default=None, description="a deploy target, model, "
                                       "service or run another screen links here about"),
        since: Optional[str] = Query(default=None),
        until: Optional[str] = Query(default=None),
        range_: Optional[str] = Query(default=None, alias="range",
                                      description="15m | 1h | 24h | 7d | 30d"),
    ) -> None:
        self.values = _filters(
            q=q, kind=kind, status=status, actor=actor, entity_type=entity_type, level=level,
            exclude_level=exclude_level, hide_verbose=hide_verbose, source=source,
            exclude_source=exclude_source, run_id=run_id, workflow_run_id=workflow_run_id,
            agent=agent, exclude_agent=exclude_agent, service=service, step_id=step_id,
            act=act, subject=subject, since=since, until=until, range_=range_)


def _not_modified(request: Request, etag: str) -> Optional[Response]:
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    return None


def _json(body: Any, etag: str) -> JSONResponse:
    from fastapi.encoders import jsonable_encoder

    return JSONResponse(content=jsonable_encoder(body),
                        headers={"ETag": etag, "Cache-Control": "no-cache"})



@router.get("/v1/activity")
@permit(P.ACTIVITY_READ, scope=Scope.org())
def list_activity(
    request: Request,
    filters: _FilterParams = Depends(),
    project_id: Optional[str] = Query(default=None),
    sort: str = Query(default="created_at"),
    order: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    cursor: Optional[str] = Query(default=None, description="next_cursor of the previous "
                                  "page: the rows strictly older than it"),
    after: Optional[str] = Query(default=None, description="newest_cursor of what you hold: "
                                 "only the rows newer than it (the live tail)"),
    total: Optional[str] = Query(default=None, description="0 to skip counting (the console "
                                 "reads its count from the histogram)"),
    summary: Optional[str] = Query(default=None, description="1: the newest non-verbose rows, "
                                   "errors first, and the error/warn counts (step panel)"),
) -> Response:
    """One page of the log. Honest empty; never fake items."""
    project_id = _resolve_project_id(project_id)
    service = _service_class()
    with begin(get_engine()) as connection:
        _require_reader(connection, project_id)
        try:
            query = service.prepare(connection, project_id, **filters.values)
            paging = json.dumps([sort, order, limit, offset, cursor, after, total, summary])
            etag = service.signature(connection, query, extra=paging)
            cached = _not_modified(request, etag)
            if cached is not None:
                return cached
            payload = service.query(
                connection, project_id, sort=sort, order=order, limit=limit, offset=offset,
                cursor=_clean(cursor), after=_clean(after),
                total=total if total is not None else True,
                summary=summary or False, **filters.values)
            _label_actors(connection, project_id, payload.get("items") or [])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _json({**payload, "project_id": project_id}, etag)


def _label_actors(connection: Any, project_id: str, items: list) -> None:
    """§12 History: each row's stored actor ("ceo") gains `actor_label` naming who it is now
    ("Param (was: ceo)"), from the identity migration's mappings. Rows are otherwise untouched."""
    try:
        from sqlalchemy import text

        from boss_engine.identity import legacy

        org = connection.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w ON "
                                      "w.id = p.workspace_id WHERE p.id = :p"), {"p": project_id}).scalar()
        if not org:
            return
        with connection.begin_nested():
            labels = legacy.actor_labels(org, [i.get("actor") for i in items if isinstance(i, dict)],
                                         db=connection)
    except Exception:  # noqa: BLE001 — an install without the identity tables: plain actors
        return
    for item in items:
        if isinstance(item, dict) and item.get("actor") in labels:
            item["actor_label"] = labels[item["actor"]]


@router.get("/v1/activity/histogram")
@permit(P.ACTIVITY_READ, scope=Scope.org())
def activity_histogram(
    request: Request,
    filters: _FilterParams = Depends(),
    project_id: Optional[str] = Query(default=None),
) -> Response:
    """Rows per time bucket, stacked by level, over the window (default 24h)."""
    project_id = _resolve_project_id(project_id)
    service = _service_class()
    values = dict(filters.values)
    if not values.get("range") and not values.get("since"):
        values["range"] = "24h"
    now = datetime.now(timezone.utc)
    with begin(get_engine()) as connection:
        _require_reader(connection, project_id)
        try:
            query = service.prepare(connection, project_id, now=now, **values)
            # A relative window gains a bucket as time passes even when nothing was logged.
            tick = int(now.timestamp() // 60) if query.range else 0
            etag = service.signature(connection, query, extra=f"histogram|{tick}")
            cached = _not_modified(request, etag)
            if cached is not None:
                return cached
            body = service.histogram(connection, project_id, now=now, **values)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _json(body, etag)


@router.get("/v1/activity/facets")
@permit(P.ACTIVITY_READ, scope=Scope.org())
def activity_facets(
    request: Request,
    filters: _FilterParams = Depends(),
    project_id: Optional[str] = Query(default=None),
) -> Response:
    """How many rows match per level, stream, agent, run, process and status — counted in SQL
    over the window, each facet ignoring its own selection. Never counted from a page."""
    project_id = _resolve_project_id(project_id)
    service = _service_class()
    with begin(get_engine()) as connection:
        _require_reader(connection, project_id)
        try:
            query = service.prepare(connection, project_id, **filters.values)
            tick = int(datetime.now(timezone.utc).timestamp() // 60) if query.range else 0
            etag = service.signature(connection, query, extra=f"facets|{tick}")
            cached = _not_modified(request, etag)
            if cached is not None:
                return cached
            body = service.facets(connection, project_id, **filters.values)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _json(body, etag)


@router.get("/v1/activity/kinds")
@permit(P.ACTIVITY_READ, scope=Scope.org())
def activity_kinds(
    filters: _FilterParams = Depends(),
    project_id: Optional[str] = Query(default=None),
    prefix: str = Query(default="", max_length=120),
    limit: int = Query(default=20, ge=1, le=50),
) -> Response:
    """`kind:` completion by prefix over the window, counted in SQL (kind:deploy →
    deploy.production), whatever the facets' top 40 hold."""
    project_id = _resolve_project_id(project_id)
    service = _service_class()
    values = {k: v for k, v in filters.values.items() if k != "kind"}
    with begin(get_engine()) as connection:
        _require_reader(connection, project_id)
        try:
            body = service.kinds(connection, project_id, prefix=prefix, limit=limit, **values)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    from fastapi.encoders import jsonable_encoder
    return JSONResponse(content=jsonable_encoder(body), headers={"Cache-Control": "no-cache"})


@router.get("/v1/activity/row/{row_id}")
@permit(P.ACTIVITY_READ, scope=Scope.org())
def activity_row(row_id: str, project_id: Optional[str] = Query(default=None)) -> dict[str, Any]:
    """One line by id (a permalink's target). 404 when it is gone or another project's."""
    project_id = _resolve_project_id(project_id)
    service = _service_class()
    with begin(get_engine()) as connection:
        _require_reader(connection, project_id)
        try:
            found = service.row(connection, project_id, row_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if found is not None:
            _label_actors(connection, project_id, [found])
    if found is None:
        raise HTTPException(status_code=404, detail="This line is gone.")
    return {"project_id": project_id, **found}


@router.get("/v1/activity/context")
@permit(P.ACTIVITY_READ, scope=Scope.org())
def activity_context(
    id: str = Query(..., description="the line to read around"),
    project_id: Optional[str] = Query(default=None),
    before: int = Query(default=20, ge=1, le=100),
    after: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """The lines just before and after one line, in the same run."""
    project_id = _resolve_project_id(project_id)
    service = _service_class()
    with begin(get_engine()) as connection:
        _require_reader(connection, project_id)
        try:
            found = service.context(connection, project_id, id, before=before, after=after)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if found is None:
        raise HTTPException(status_code=404, detail="This line is gone.")
    return {"project_id": project_id, **found}


_CSV_COLUMNS = ("created_at", "level", "status", "source", "kind", "title", "actor", "agent",
                "service", "run_id", "workflow_run_id", "entity_type", "entity_id", "body",
                "data")


@router.get("/v1/activity/export")
@permit(P.ACTIVITY_EXPORT, scope=Scope.org())
def activity_export(
    filters: _FilterParams = Depends(),
    project_id: Optional[str] = Query(default=None),
    format: str = Query(default="ndjson", pattern="^(csv|ndjson)$"),
) -> StreamingResponse:
    """The current filter, redacted, newest first, at most 50,000 lines and 30 days.

    Streamed straight from the keyset pages, never held in memory. `X-Truncated: 1` is decided
    before the first byte by counting the filter in SQL."""
    from boss_engine.services.activity_log import EXPORT_MAX_ROWS
    from boss_engine.services.redaction import redact_secrets

    project_id = _resolve_project_id(project_id)
    service = _service_class()
    values = dict(filters.values)
    with begin(get_engine()) as connection:
        _require_reader(connection, project_id)
        try:
            values = service.export_params(project_id, **values)
            query = service.prepare(connection, project_id, **values)
            counted, _exact = service._count(connection, query)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    truncated = counted > EXPORT_MAX_ROWS

    def items() -> Iterator[dict[str, Any]]:
        with begin(get_engine()) as stream:
            yield from service.export(stream, project_id, max_rows=EXPORT_MAX_ROWS, **values)

    def ndjson() -> Iterator[str]:
        for item in items():
            # A final scrub of the line as written: the file is the last boundary.
            yield redact_secrets(json.dumps(item, default=str)) + "\n"

    def as_csv() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(_CSV_COLUMNS)
        yield buffer.getvalue()
        for item in items():
            buffer.seek(0)
            buffer.truncate()
            writer.writerow([
                json.dumps(item.get(c), default=str) if c == "data" else item.get(c, "")
                for c in _CSV_COLUMNS])
            yield redact_secrets(buffer.getvalue())

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    headers = {"Content-Disposition": f'attachment; filename="boss-log-{stamp}.{format}"',
               "X-Truncated": "1" if truncated else "0",
               "X-Row-Count": str(min(counted, EXPORT_MAX_ROWS))}
    if format == "csv":
        return StreamingResponse(as_csv(), media_type="text/csv; charset=utf-8", headers=headers)
    return StreamingResponse(ndjson(), media_type="application/x-ndjson", headers=headers)


__all__ = ["router"]


@router.post("/v1/activity/retention/prune")
# The worker's hourly call (worker protocol), not the Owner's activity:prune: a person never
# calls it, and activity:prune is Owner-only, so the worker could not (identity int-b).
@permit(P.WORKER_LEASE)
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

