"""GET /v1/events/stream — realtime event push, authorized per event (BOSSAPP-835).

The route's declaration checks the viewer may read activity where they asked (the project named,
or the org). That is only the door: every event is then checked against THIS viewer's permissions
at THAT event's project, for THAT event's kind (boss_engine.services.event_stream), before a byte
of it is written to the connection.

    ?project_id=   only this project's events
    ?kinds=        comma list: exact kinds ("decision.created") or prefixes ("decision.", "code_task*")
    Last-Event-ID  (header, or ?last_event_id=) replay the durable events after that one first
    ?max_seconds=  close after this long (1..1800, default 600); EventSource reconnects with
                   Last-Event-ID, so nothing is lost across the reconnect

Overflow (a slow reader) and a listener gap send ``event: resync``: refetch what you show.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from boss_engine.identity import P, Scope, permit
from boss_engine.services import event_stream as es

from ..db import get_engine

router = APIRouter(tags=["events"])


def viewer_for(request: Request, engine) -> object:
    """Who is reading, captured NOW (the stream outlives the request's context variables): the
    verified identity context when there is one, else the legacy member rule's principal."""
    from boss_engine.identity import lineage

    ctx = getattr(request.state, "identity", None) or lineage.current_context()
    if ctx is not None:
        return es.IdentityViewer(ctx, engine)
    from apps.api.app.main import current_api_principal, current_transport_authenticated

    return es.LegacyViewer(current_api_principal.get(), bool(current_transport_authenticated.get()),
                           engine)


@router.get("/v1/events/stream")
@permit(P.ACTIVITY_READ, scope=Scope.project_or_org(query="project_id"))
async def stream_events(
    request: Request,
    project_id: Optional[str] = Query(default=None),
    kinds: Optional[str] = Query(default=None, max_length=1000),
    last_event_id: Optional[str] = Query(default=None, max_length=200),
    max_seconds: int = Query(default=600, ge=1, le=1800),
):
    """Server-sent events: one `data:` JSON object per event the viewer may see."""
    engine = get_engine()
    viewer = viewer_for(request, engine)
    interest = es.Interest.parse(kinds, project_id)
    anchor = (request.headers.get("last-event-id") or last_event_id or "").strip() or None
    hub = es.get_hub(engine)
    return StreamingResponse(
        es.stream(engine, hub, viewer, interest, last_event_id=anchor,
                  max_seconds=float(max_seconds)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


__all__ = ["router", "viewer_for"]
