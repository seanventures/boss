"""Learning while executing, live and measured (BOSSAPP-768, KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §4.4, §4.6).

GET /v1/knowledge/view/live    what agents learned, the rules they proposed and the consulted /
                               reused edges they drew since a cursor, for one run or the project
                               (KgLiveResponse). The Graph area and every run's "Learned so far"
                               strip poll it only while visible and while a run is writing.
GET /v1/knowledge/view/conversation  one conversation's learnings written and used, and the
                               names of the graph items its transcript cites (read-only).
GET /v1/knowledge/view/proof   one run's scorecard (steps, turns, tokens, cost, wall time, repair
                               rounds, what it consulted and reused), or a pair compared: the
                               later run against a baseline, attributed to learning only when it
                               used what the baseline learned (KgRunProofResponse).

A sibling of routers/knowledge_view.py and knowledge_rules.py. Reads only, under knowledge:read;
decisions and precedents also need decision:read and are absent without it (never sent, and no
edge to one either), as in the rest of the Graph area.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services.knowledge.learning_conversations import mend_names
from boss_engine.services.knowledge import learning_live

router = APIRouter(prefix="/v1/knowledge/view", tags=["knowledge"])
_settings = get_settings()
_engine = get_engine(_settings)


def _read(request: Request, project_id: str, actor: str, read):
    from apps.api.app.routers.knowledge_view import _actor, _http, _withheld

    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor)
            # A conversation still named by its thread id is named on read (mend_names).
            return mend_names(connection, read(connection, _withheld(request, connection, project_id, actor)))
    except HTTPException:
        raise
    except Exception as error:  # noqa: BLE001 — mapped to HTTP
        raise _http(error) from error


@router.get("/live")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def live(
    request: Request,
    project_id: str = Query(...),
    actor: str = Query(...),
    since: Optional[str] = Query(None, description="The cursor the previous read returned."),
    run: Optional[str] = Query(None, description="A build (crun_…) or a workflow run (wfr_…)."),
) -> dict[str, Any]:
    """KgLiveResponse: learnings, rules and edges since the cursor; ``active_runs`` says whether
    to poll again (every ``poll_after_ms``)."""
    return _read(request, project_id, actor, lambda c, withheld: learning_live.live(
        c, project_id, since=since, run=run, withheld=withheld))


@router.get("/proof")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def proof(
    request: Request,
    project_id: str = Query(...),
    actor: str = Query(...),
    run: str = Query(..., description="The run to score (the later one of a pair)."),
    baseline: Optional[str] = Query(None, description="The earlier or control run to compare it with."),
) -> dict[str, Any]:
    """KgRunProofResponse: ``run`` alone, or ``run`` against ``baseline``."""
    def read(connection, withheld):
        if baseline:
            return {"comparison": learning_live.compare(connection, project_id, run=run, baseline=baseline,
                                                        withheld=withheld), "run": None}
        return {"comparison": None,
                "run": learning_live.run_proof(connection, project_id, run, withheld=withheld)}

    return _read(request, project_id, actor, read)


@router.get("/conversation")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def conversation(
    request: Request,
    project_id: str = Query(...),
    actor: str = Query(...),
    thread: str = Query(..., description="The conversation (athr_…)."),
) -> dict[str, Any]:
    """KgConversationKnowledge: what one conversation wrote to the graph and the earlier items it
    used, and the title of every graph item its transcript names (read-only; the console's
    conversation view). Withheld kinds are absent, as everywhere in the Graph area."""
    from boss_engine.services.knowledge.learning_conversations import conversation_knowledge

    return _read(request, project_id, actor, lambda c, withheld: conversation_knowledge(
        c, project_id, thread, withheld=withheld))
