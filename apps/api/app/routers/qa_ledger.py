"""THE TEST LEDGER over HTTP: `boss qa report` reads it (engine: services/qa_ledger.py).

GET /v1/qa/ledger?project_id=P[&since=ISO][&case=REC-01]   one row per attempt at a fault case
                                                           (read-only; free text scrubbed as the
                                                           diagnostics report scrubs it)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit

router = APIRouter(tags=["qa"])


@router.get("/v1/qa/ledger")
@permit(P.CODE_TASK_READ, scope=Scope.project(query="project_id"))
def get_qa_ledger(project_id: str, since: Optional[datetime] = None, case: Optional[str] = None,
                  limit: int = 200) -> dict[str, Any]:
    from boss_engine.services import qa_ledger
    from boss_engine.services.project_experience.common import require_project

    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    with begin(get_engine()) as connection:
        try:
            require_project(connection, project_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from None
        try:
            out = qa_ledger.ledger(connection, project_id, since=since, case=case, limit=limit)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
    return {**out, "markdown": qa_ledger.as_markdown(out["rows"])}
