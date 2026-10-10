"""Architecture approval: read a run's architecture documents (services/architecture_docs.py).

Two reads. Answering — approve, request changes with a note, reject — goes through the decision
endpoints every approval uses (`/v1/decisions/{id}/approve|revise|reject`), so the Approvals
page, MCP and the step's own panel all hit the one place that refuses an agent's approval.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


def build_architecture_router(*, engine, require_reader: Callable[..., None]) -> APIRouter:
    from boss_engine.services import architecture_docs

    router = APIRouter(tags=["workflows"])

    @router.get("/v1/architecture-reviews")
    @permit(P.WORKFLOW_READ, scope=Scope.project(query="project_id"))
    def list_architecture_reviews(project_id: str, workflow_run_id: Optional[str] = None,
                                  limit: int = 50) -> dict[str, Any]:
        """The architecture reviews of a project, or of one run: status in words, the version
        waiting, and the buttons its decision offers."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            items = architecture_docs.list_reviews(
                connection, project_id, workflow_run_id=workflow_run_id, limit=limit)
        return {"items": items, "sections": [
            {"key": k, "heading": h} for k, h, _w in architecture_docs.SECTIONS]}

    @router.get("/v1/architecture-reviews/{review_id}")
    @permit(P.WORKFLOW_READ, scope=Scope.project(query="project_id"))
    def get_architecture_review(review_id: str, project_id: str) -> dict[str, Any]:
        """One review with every version's document, the current one first in `markdown`."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            review = architecture_docs.get_review(connection, project_id, review_id)
        if review is None:
            raise HTTPException(status_code=404, detail="Architecture review not found.")
        return review

    return router
