"""KG P7a: a regulation document and its ontology pack, over HTTP.

GET/POST /v1/knowledge/packs      the project's active ontology pack
GET/POST /v1/knowledge/documents  a regulation document and its clauses

Manage calls require knowledge:manage and step_up=confirmed in the body, the same
confirmation shape as routers/knowledge.py's ManageBody. Refusals are in words: no pack set,
an unknown pack, or a clause missing its section/text are all 422s naming what to fix.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services.knowledge import documents
from boss_engine.services.knowledge import ontology_packs
from boss_engine.services.knowledge.ontology_packs import PackRefused
from boss_engine.services.knowledge.documents import DocumentRefused
from boss_engine.services.knowledge.service import MANAGE, READ, require_confirmation

router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])
_settings = get_settings()
_engine = get_engine(_settings)


def _actor(connection, project_id: str, actor: str, permission: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, permission)


def _http(error: Exception) -> HTTPException:
    if isinstance(error, HTTPException):
        return error
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, (PackRefused, DocumentRefused, ValueError)):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=str(error))
    return HTTPException(status_code=500, detail=error.__class__.__name__)


class SetPackBody(BaseModel):
    project_id: str
    actor: str
    step_up: Optional[str] = None
    pack_id: str


class ClauseBody(BaseModel):
    section: str
    text: str
    citation: Optional[str] = None
    url: Optional[str] = None


class AddDocumentBody(BaseModel):
    project_id: str
    actor: str
    step_up: Optional[str] = None
    title: str
    source_url: Optional[str] = ""
    citation: Optional[str] = ""
    clauses: list[dict]
    sensitivity: Optional[str] = "public"


@router.get("/packs")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def get_packs(project_id: str = Query(...), actor: str = Query(...)) -> dict[str, Any]:
    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            current = ontology_packs.get_project_pack(connection, project_id=project_id)
            available = [
                {"pack_id": pid, "version": pack.version}
                for pid, pack in ontology_packs.shipped_packs().items()
            ]
            return {
                "current": ({"pack_id": current.pack_id, "version": current.version}
                           if current else None),
                "available": available,
            }
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


@router.post("/packs")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def set_pack(body: SetPackBody) -> dict[str, Any]:
    try:
        require_confirmation(body.step_up)
    except PermissionError as error:
        # A missing confirmation is a 422 (criterion 1: "without it, 403/422 respectively" —
        # the wrong-permission 403 is the permit gate; a missing step_up is a 422 in words).
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, MANAGE)
            pack = ontology_packs.set_project_pack(
                connection, project_id=body.project_id, pack_id=body.pack_id, set_by=body.actor,
            )
            return {"pack_id": pack.pack_id, "version": pack.version}
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


@router.get("/documents")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def list_documents_route(project_id: str = Query(...), actor: str = Query(...)) -> dict[str, Any]:
    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return {"documents": documents.list_documents(connection, project_id=project_id)}
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


@router.get("/documents/{clause_id}/linked-rules")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def linked_rules_route(
    clause_id: str, project_id: str = Query(...), actor: str = Query(...),
) -> dict[str, Any]:
    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            rules = documents.linked_rules(connection, project_id=project_id, clause_id=clause_id)
            return {"rules": rules}
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


@router.post("/documents")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def add_document_route(body: AddDocumentBody) -> dict[str, Any]:
    try:
        require_confirmation(body.step_up)
    except PermissionError as error:
        # A missing confirmation is a 422 (criterion 1: "without it, 403/422 respectively" —
        # the wrong-permission 403 is the permit gate; a missing step_up is a 422 in words).
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, MANAGE)
            return documents.add_document(
                connection, project_id=body.project_id, title=body.title,
                source_url=body.source_url or "", citation=body.citation or "",
                clauses=body.clauses, sensitivity=body.sensitivity or "public",
                actor_kind="person", actor_ref=body.actor,
            )
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error
