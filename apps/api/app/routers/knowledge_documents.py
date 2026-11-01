"""KG P7a: a regulation document and its ontology pack, over HTTP.

GET/POST /v1/knowledge/packs      the project's active ontology pack
GET/POST /v1/knowledge/documents  a regulation document and its clauses
POST /v1/knowledge/documents/uploads[/{id}/edit|confirm|discard]  upload a PDF or text file:
                                  sections proposed, a person confirms before anything is written
GET/POST /v1/knowledge/samples/dq-rulebook  the NorthPeak DQ rulebook through the same upload

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


class LinkRuleBody(BaseModel):
    project_id: str
    actor: str
    step_up: Optional[str] = None
    rule_id: str


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
            held = ontology_packs.get_project_packs(connection, project_id=project_id)
            available = [
                {"pack_id": pid, "version": pack.version}
                for pid, pack in ontology_packs.shipped_packs().items()
            ]
            return {
                # The pack set last (a project holds several; `packs` lists them all).
                "current": ({"pack_id": current.pack_id, "version": current.version}
                           if current else None),
                "packs": [{"pack_id": p.pack_id, "version": p.version,
                           "includes": [i for i in p.pack_ids if i != p.pack_id]}
                          for p in held],
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
            # The workflows the pack completes are installed now, as the set_ontology_pack tool
            # does (one path): the pack is set either way; boot seeds again on a failure.
            from boss_engine.services.workflow_catalogue import seed_now_unlocked

            try:
                with connection.begin_nested():
                    seed_now_unlocked(connection, body.project_id, actor=body.actor)
            except Exception:  # noqa: BLE001
                pass
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


@router.post("/documents/{clause_id}/link-rule")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def link_rule_route(clause_id: str, body: LinkRuleBody) -> dict[str, Any]:
    try:
        require_confirmation(body.step_up)
    except PermissionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, MANAGE)
            return documents.link_rule(
                connection, project_id=body.project_id, clause_id=clause_id,
                rule_id=body.rule_id, actor=body.actor)
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


# ── Upload a document (rulebook source G3): stage → preview → (edit) → a PERSON confirms ──────
# Nothing reaches the graph before the confirmation (operator decision 2026-11-01, amending SPEC
# resolution #1). The file travels as base64 in JSON, so the confirmation shape and the permit
# gate read it exactly as every other manage call.

class UploadBody(BaseModel):
    project_id: str
    actor: str
    step_up: Optional[str] = None
    filename: str
    content_base64: str
    title: Optional[str] = None
    pattern: Optional[str] = None


class UploadEditBody(BaseModel):
    project_id: str
    actor: str
    step_up: Optional[str] = None
    edits: list[dict] = []
    title: Optional[str] = None


class UploadConfirmBody(BaseModel):
    project_id: str
    actor: str
    step_up: Optional[str] = None
    title: Optional[str] = None


def _confirmed(step_up: Optional[str]) -> None:
    try:
        require_confirmation(step_up)
    except PermissionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _upload_http(error: Exception) -> HTTPException:
    from boss_engine.services.knowledge.document_upload import UploadRefused

    if isinstance(error, UploadRefused):
        return HTTPException(status_code=422, detail=str(error))
    return _http(error)


@router.post("/documents/uploads")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def stage_upload_route(body: UploadBody) -> dict[str, Any]:
    import base64
    import binascii

    from boss_engine.services.knowledge import document_upload

    _confirmed(body.step_up)
    try:
        payload = base64.b64decode(body.content_base64 or "", validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(status_code=422, detail="The file did not arrive whole: send it again.") from error
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, MANAGE)
            return document_upload.stage_upload(
                connection, project_id=body.project_id, payload=payload, filename=body.filename,
                title=body.title, pattern=body.pattern, actor_kind="person", actor_ref=body.actor)
    except Exception as error:  # noqa: BLE001
        raise _upload_http(error) from error


@router.get("/documents/uploads")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def pending_uploads_route(project_id: str = Query(...), actor: str = Query(...)) -> dict[str, Any]:
    from boss_engine.services.knowledge import document_upload

    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return {"uploads": document_upload.pending_uploads(connection, project_id=project_id)}
    except Exception as error:  # noqa: BLE001
        raise _upload_http(error) from error


@router.get("/documents/uploads/{upload_id}")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def get_upload_route(upload_id: str, project_id: str = Query(...), actor: str = Query(...)) -> dict[str, Any]:
    from boss_engine.services.knowledge import document_upload

    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return document_upload.get_upload(connection, project_id=project_id, upload_id=upload_id)
    except Exception as error:  # noqa: BLE001
        raise _upload_http(error) from error


@router.get("/documents/uploads/{upload_id}/sections/{key}")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def upload_section_route(upload_id: str, key: str, project_id: str = Query(...),
                         actor: str = Query(...)) -> dict[str, Any]:
    from boss_engine.services.knowledge import document_upload

    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return {"key": key, "lines": document_upload.section_lines(
                connection, project_id=project_id, upload_id=upload_id, key=key)}
    except Exception as error:  # noqa: BLE001
        raise _upload_http(error) from error


@router.post("/documents/uploads/{upload_id}/edit")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def edit_upload_route(upload_id: str, body: UploadEditBody) -> dict[str, Any]:
    from boss_engine.services.knowledge import document_upload

    _confirmed(body.step_up)
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, MANAGE)
            return document_upload.edit_upload(connection, project_id=body.project_id,
                                               upload_id=upload_id, edits=body.edits, title=body.title)
    except Exception as error:  # noqa: BLE001
        raise _upload_http(error) from error


@router.post("/documents/uploads/{upload_id}/confirm")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def confirm_upload_route(upload_id: str, body: UploadConfirmBody) -> dict[str, Any]:
    from boss_engine.services.knowledge import document_upload

    _confirmed(body.step_up)
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, MANAGE)
            return document_upload.confirm_upload(connection, project_id=body.project_id,
                                                  upload_id=upload_id, actor_kind="person",
                                                  actor_ref=body.actor, title=body.title)
    except Exception as error:  # noqa: BLE001
        raise _upload_http(error) from error


@router.post("/documents/uploads/{upload_id}/discard")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def discard_upload_route(upload_id: str, body: UploadConfirmBody) -> dict[str, Any]:
    from boss_engine.services.knowledge import document_upload

    _confirmed(body.step_up)
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, MANAGE)
            return document_upload.discard_upload(connection, project_id=body.project_id,
                                                  upload_id=upload_id, actor_ref=body.actor)
    except Exception as error:  # noqa: BLE001
        raise _upload_http(error) from error


# ── Case study 4: the AML sample (services/knowledge/aml_sample.py) ──────────────────────────
# One product action loads it: the banking-aml pack, the sample's three mock connectors, the
# synthetic bank into the Knowledge Graph, and the aml_alert_investigation workflow installed.
# The same action the agent's `load_aml_sample` tool takes; never a SQL script.

class LoadSampleBody(BaseModel):
    project_id: str
    actor: str
    step_up: Optional[str] = None


@router.get("/samples/aml")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def aml_sample_status(project_id: str = Query(...), actor: str = Query(...)) -> dict[str, Any]:
    from boss_engine.services.knowledge import aml_sample

    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return aml_sample.status(connection, project_id=project_id)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


@router.post("/samples/aml")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def load_aml_sample(body: LoadSampleBody) -> dict[str, Any]:
    from boss_engine.services.knowledge import aml_sample

    try:
        require_confirmation(body.step_up)
    except PermissionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, MANAGE)
            return aml_sample.load_sample(connection, project_id=body.project_id,
                                          actor=body.actor)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


# ── DQ sample (rulebook source G7): the shipped rulebook through the same upload path ──────────

@router.get("/samples/dq-rulebook")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def dq_rulebook_status(project_id: str = Query(...), actor: str = Query(...)) -> dict[str, Any]:
    from boss_engine.services.knowledge import dq_rulebook_sample

    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return dq_rulebook_sample.status(connection, project_id=project_id)
    except Exception as error:  # noqa: BLE001
        raise _upload_http(error) from error


@router.post("/samples/dq-rulebook")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def stage_dq_rulebook(body: LoadSampleBody) -> dict[str, Any]:
    from boss_engine.services.knowledge import dq_rulebook_sample

    _confirmed(body.step_up)
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, MANAGE)
            return dq_rulebook_sample.stage(connection, project_id=body.project_id,
                                            actor_kind="person", actor_ref=body.actor)
    except Exception as error:  # noqa: BLE001
        raise _upload_http(error) from error


# ── The DQ case study: NorthPeak Bank's data platform (services/knowledge/dq_platform.py) ─────
# One product action loads it: the banking-dq pack (added beside the project's other packs), the
# Eagle and Olympus connectors, the platform into the Knowledge Graph (never the rulebook's
# rules), the DQ workflows and the Escalation Agent's daily schedule. Same action as the agent's
# `load_dq_platform` tool.

@router.get("/samples/dq")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def dq_platform_status(project_id: str = Query(...), actor: str = Query(...),
                       cob: Optional[str] = Query(None)) -> dict[str, Any]:
    from boss_engine.services.knowledge import dq_platform

    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return dq_platform.status(connection, project_id=project_id, cob=cob or None)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


@router.post("/samples/dq")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def load_dq_platform(body: LoadSampleBody) -> dict[str, Any]:
    from boss_engine.services.knowledge import dq_platform

    try:
        require_confirmation(body.step_up)
    except PermissionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, MANAGE)
            return dq_platform.load_platform(connection, project_id=body.project_id,
                                             actor=body.actor)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


@router.post("/samples/dq/schedule")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def schedule_dq_intake(body: LoadSampleBody) -> dict[str, Any]:
    """The scheduled trigger: each morning, an RCA per exception group Eagle graded Error."""
    from boss_engine.services.knowledge import dq_platform

    try:
        require_confirmation(body.step_up)
    except PermissionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, MANAGE)
            return dq_platform.schedule_intake(connection, project_id=body.project_id,
                                               actor=body.actor)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error
