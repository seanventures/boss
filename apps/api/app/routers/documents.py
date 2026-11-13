"""Documents with revisions, conflict detection and anchored comments over HTTP (BOSSAPP-874;
services/documents.py).

WHO MAY DO WHAT: reading is work_item:read; every change is work_item:manage, attributed to the
signed-in member (require_write_actor with task_create) — never to a claimed actor. A save names
the revision it was made from; a stale base answers 409 with the current revision so the writer
can merge, and so does someone else's edit lock.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None


class CreateBody(Body):
    title: str = Field(min_length=1, max_length=300)
    kind: str = Field(default="note", pattern="^(prd|plan|minutes|architecture|note)$")
    body: str = Field(default="", max_length=400_000)
    work_item_id: Optional[str] = Field(default=None, max_length=200)


class FromArtifactBody(Body):
    artifact_id: str = Field(min_length=1, max_length=200)
    work_item_id: Optional[str] = Field(default=None, max_length=200)


class SaveBody(Body):
    body: str = Field(max_length=400_000)
    base_revision_id: Optional[str] = Field(default=None, max_length=200)
    note: str = Field(default="", max_length=500)


class RestoreBody(Body):
    revision_no: int = Field(ge=1)
    base_revision_id: Optional[str] = Field(default=None, max_length=200)


class LockBody(Body):
    release: bool = False


class ThreadBody(Body):
    revision_id: str = Field(min_length=1, max_length=200)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=8000)


class ReplyBody(Body):
    body: str = Field(min_length=1, max_length=8000)


class ResolveBody(Body):
    resolved: bool = True


def build_documents_router(*, engine, require_reader: Callable[..., None],
                           require_write_actor: Callable[..., str],
                           viewing: Optional[Callable[[Any], None]] = None) -> APIRouter:
    from boss_engine.services import documents as docs
    from boss_engine.services.work_items import WorkItemError

    router = APIRouter(tags=["documents"])

    def _see(c: Any) -> None:
        if viewing is not None:
            viewing(c)

    def _errors(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except docs.DocumentConflict as exc:
            # The writer gets the revision that won, so it can merge without another round trip.
            # Raised (not returned) so the transaction rolls back: a refused save writes nothing.
            raise HTTPException(status_code=409, detail={
                "message": str(exc), "locked": isinstance(exc, docs.DocumentLocked),
                "current": exc.current}) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (docs.DocumentError, WorkItemError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _read(project_id: str, fn: Callable[[Any], Any]) -> Any:
        with begin(engine) as c:
            require_reader(c, project_id)
            _see(c)
            return _errors(lambda: fn(c))

    def _write(payload: Body, fn: Callable[[Any, str], Any]) -> Any:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "task_create")
            _see(c)
            return _errors(lambda: fn(c, actor))

    @router.get("/v1/documents")
    @permit(P.WORK_ITEM_READ, scope=Scope.project(query="project_id"))
    def list_documents(project_id: str, work_item_id: Optional[str] = None) -> dict:
        return {"documents": _read(project_id, lambda c: docs.list_documents(
            c, project_id, work_item_id=work_item_id))}

    @router.post("/v1/documents", status_code=201)
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def create_document(payload: CreateBody) -> Any:
        return _wrap(_write(payload, lambda c, actor: docs.create_document(
            c, project_id=payload.project_id, title=payload.title, body=payload.body,
            kind=payload.kind, work_item_id=payload.work_item_id, actor=actor)))

    @router.post("/v1/documents/from-artifact")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def from_artifact(payload: FromArtifactBody) -> Any:
        return _wrap(_write(payload, lambda c, actor: docs.ensure_for_artifact(
            c, project_id=payload.project_id, artifact_id=payload.artifact_id, actor=actor,
            work_item_id=payload.work_item_id)))

    @router.get("/v1/documents/{document_id}")
    @permit(P.WORK_ITEM_READ, scope=Scope.project(query="project_id"))
    def get_document(document_id: str, project_id: str) -> dict:
        return {"document": _read(project_id, lambda c: docs.get_document(c, project_id, document_id))}

    @router.get("/v1/documents/{document_id}/revisions/{revision_no}")
    @permit(P.WORK_ITEM_READ, scope=Scope.project(query="project_id"))
    def get_revision(document_id: str, revision_no: int, project_id: str) -> dict:
        return {"revision": _read(project_id, lambda c: docs.get_revision(
            c, project_id, document_id, revision_no))}

    @router.get("/v1/documents/{document_id}/diff")
    @permit(P.WORK_ITEM_READ, scope=Scope.project(query="project_id"))
    def diff(document_id: str, project_id: str, from_no: int = Query(..., alias="from", ge=1),
             to_no: Optional[int] = Query(None, alias="to", ge=1)) -> dict:
        return _read(project_id, lambda c: docs.diff(c, project_id, document_id,
                                                      from_no=from_no, to_no=to_no))

    @router.post("/v1/documents/{document_id}/revisions")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def save(document_id: str, payload: SaveBody) -> Any:
        return _wrap(_write(payload, lambda c, actor: docs.save_revision(
            c, project_id=payload.project_id, document_id=document_id, body=payload.body,
            base_revision_id=payload.base_revision_id, actor=actor, note=payload.note)))

    @router.post("/v1/documents/{document_id}/restore")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def restore(document_id: str, payload: RestoreBody) -> Any:
        return _wrap(_write(payload, lambda c, actor: docs.restore(
            c, project_id=payload.project_id, document_id=document_id,
            revision_no=payload.revision_no, base_revision_id=payload.base_revision_id,
            actor=actor)))

    @router.post("/v1/documents/{document_id}/lock")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def lock(document_id: str, payload: LockBody) -> Any:
        return _wrap(_write(payload, lambda c, actor: docs.lock(
            c, project_id=payload.project_id, document_id=document_id, actor=actor,
            release=payload.release)))

    @router.post("/v1/documents/{document_id}/threads", status_code=201)
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def add_thread(document_id: str, payload: ThreadBody) -> Any:
        return {"thread": _write(payload, lambda c, actor: docs.add_thread(
            c, project_id=payload.project_id, document_id=document_id,
            revision_id=payload.revision_id, start=payload.start, end=payload.end,
            body=payload.body, actor=actor))}

    @router.post("/v1/documents/{document_id}/threads/{thread_id}/replies")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def reply(document_id: str, thread_id: str, payload: ReplyBody) -> Any:
        return {"thread": _write(payload, lambda c, actor: docs.reply(
            c, project_id=payload.project_id, document_id=document_id, thread_id=thread_id,
            body=payload.body, actor=actor))}

    @router.post("/v1/documents/{document_id}/threads/{thread_id}/resolve")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def resolve(document_id: str, thread_id: str, payload: ResolveBody) -> Any:
        return {"thread": _write(payload, lambda c, actor: docs.set_thread_status(
            c, project_id=payload.project_id, document_id=document_id, thread_id=thread_id,
            resolved=payload.resolved, actor=actor))}

    return router


def _wrap(out: Any) -> dict:
    return {"document": out}
