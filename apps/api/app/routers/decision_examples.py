"""Decision examples over HTTP (BOSSAPP-893, services/decision_examples.py).

    GET    /v1/decision-examples                  the project's examples (overrides and saved)
    GET    /v1/decision-examples/export           the same, anonymized, as JSONL
    POST   /v1/decisions/{decision_id}/example    Save as example (an answered decision)
    DELETE /v1/decision-examples/{example_id}     drop one that should not be learned from

Overrides are captured by the decision service itself; nothing here needs to be called for them.
Mounted from main.py with the auth helpers injected.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit

_RESOLVE = "decision_resolve"


class SaveBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    label: str = ""
    note: str = ""


def build_decision_examples_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
) -> APIRouter:
    from boss_engine.services import decision_examples as dx

    router = APIRouter(tags=["decisions"])

    def _fail(error: Exception):
        if isinstance(error, dx.ExampleError):
            raise HTTPException(status_code=getattr(error, "status", 422),
                                detail=str(error)) from error
        raise error

    @router.get("/v1/decision-examples")
    @permit(P.DECISION_READ, scope=Scope.project(query="project_id"))
    def list_examples(project_id: str, source: Optional[str] = None) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            examples = dx.list_examples(connection, project_id, source=source)
        return {"project_id": project_id, "examples": examples,
                "counts": {s: sum(1 for e in examples if e["source"] == s) for s in dx.SOURCES}}

    @router.get("/v1/decision-examples/export")
    @permit(P.DECISION_READ, scope=Scope.project(query="project_id"))
    def export(project_id: str, source: Optional[str] = None) -> PlainTextResponse:
        """One anonymized example per line: no secret, no person, no project id."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            body = dx.export_jsonl(connection, project_id, source=source)
        return PlainTextResponse(body, media_type="application/x-ndjson", headers={
            "Content-Disposition": 'attachment; filename="decision-examples.jsonl"'})

    @router.post("/v1/decisions/{decision_id}/example")
    @permit(P.DECISION_APPROVE, scope=Scope.of("decision", path="decision_id"))
    def save(decision_id: str, body: SaveBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, _RESOLVE)
            try:
                return dx.save(connection, project_id=body.project_id, decision_id=decision_id,
                               actor=actor, label=body.label, note=body.note)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.delete("/v1/decision-examples/{example_id}")
    @permit(P.DECISION_APPROVE, scope=Scope.project(query="project_id"))
    def delete(example_id: str, project_id: str, actor: Optional[str] = None) -> dict[str, Any]:
        with begin(engine) as connection:
            who = require_write_actor(connection, project_id, actor, _RESOLVE)
            if not dx.delete(connection, project_id=project_id, example_id=example_id,
                             actor=who):
                raise HTTPException(status_code=404, detail="no such example in this project")
        return {"deleted": example_id}

    return router
