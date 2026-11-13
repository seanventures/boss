"""The tool-call audit over HTTP (BOSSAPP-819): every tool call BOSS decided, as it recorded it.

GET /v1/tool-calls          one page, newest first, keyset (`cursor` = the last page's `next`)
GET /v1/tool-calls/export   the same filter as NDJSON, streamed, at most 50,000 rows

Filters: run, server, tool, decision, outcome, screen (the injection screen's verdict:
clean | suspicious | unscreened, or `none` for a call with no result), since / until (ISO times).
The rows are the append-only `tool_call_audit` table (services/tool_call_audit.py): arguments
are the redacted preview, never the raw call. Reading is activity:read in the project; the file
is activity:export, like the Activity log's own export.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


def build_tool_calls_router(*, engine, require_reader: Callable[..., None]) -> APIRouter:
    from boss_engine.services import tool_call_audit as audit

    router = APIRouter(tags=["tool-calls"])

    def _filters(run_id, server_id, tool, decision, outcome, screen, since, until) -> dict:
        try:
            return audit.tool_call_filters(run_id=run_id, server_id=server_id, tool=tool,
                                           decision=decision, outcome=outcome, screen=screen,
                                           since=since, until=until)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/v1/tool-calls")
    @permit(P.ACTIVITY_READ, scope=Scope.project(query="project_id"))
    def list_tool_calls(
        project_id: str = Query(..., min_length=1, max_length=200),
        run_id: Optional[str] = Query(default=None, max_length=200),
        server_id: Optional[str] = Query(default=None, max_length=200),
        tool: Optional[str] = Query(default=None, max_length=300),
        decision: Optional[str] = Query(default=None, max_length=40),
        outcome: Optional[str] = Query(default=None, max_length=40),
        screen: Optional[str] = Query(default=None, max_length=20),
        since: Optional[str] = Query(default=None, max_length=40),
        until: Optional[str] = Query(default=None, max_length=40),
        cursor: Optional[str] = Query(default=None, max_length=200),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        filters = _filters(run_id, server_id, tool, decision, outcome, screen, since, until)
        with begin(engine) as c:
            require_reader(c, project_id)
            try:
                return audit.query_tool_calls(c, project_id=project_id, cursor=cursor,
                                              limit=limit, **filters)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/v1/tool-calls/export")
    @permit(P.ACTIVITY_EXPORT, scope=Scope.project(query="project_id"))
    def export_tool_calls(
        project_id: str = Query(..., min_length=1, max_length=200),
        run_id: Optional[str] = Query(default=None, max_length=200),
        server_id: Optional[str] = Query(default=None, max_length=200),
        tool: Optional[str] = Query(default=None, max_length=300),
        decision: Optional[str] = Query(default=None, max_length=40),
        outcome: Optional[str] = Query(default=None, max_length=40),
        screen: Optional[str] = Query(default=None, max_length=20),
        since: Optional[str] = Query(default=None, max_length=40),
        until: Optional[str] = Query(default=None, max_length=40),
    ) -> StreamingResponse:
        """NDJSON, newest first, streamed page by page (never held in memory)."""
        filters = _filters(run_id, server_id, tool, decision, outcome, screen, since, until)
        with begin(engine) as c:
            require_reader(c, project_id)

        def lines() -> Iterator[str]:
            with begin(engine) as stream:
                for row in audit.export_tool_calls(stream, project_id=project_id, **filters):
                    yield json.dumps(row, default=str) + "\n"

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return StreamingResponse(
            lines(), media_type="application/x-ndjson",
            headers={"Content-Disposition":
                     f'attachment; filename="boss-tool-calls-{stamp}.ndjson"'})

    return router


__all__ = ["build_tool_calls_router"]
