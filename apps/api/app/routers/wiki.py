"""The project wiki over HTTP (BOSSAPP-891, services/wiki.py).

    GET  /v1/wiki/pages                       the project's pages (title, revision, who, pending)
    GET  /v1/wiki/pages/{slug}                one page: body, links, backlinks, pending changes
    GET  /v1/wiki/pages/{slug}/revisions      its history, newest first
    PUT  /v1/wiki/pages/{slug}                a person's edit (its own consent); an agent's 403
    POST /v1/wiki/pages/{slug}/restore        an old revision made current, as a new revision
    POST /v1/wiki/pages/{slug}/propose        a change parked for a person (diff-bound consent)
    POST /v1/wiki/maintain                    render the knowledge graph's facts into the wiki

Every write binds the signed-in principal through `require_write_actor`, so a console edit is a
PERSON's edit and an agent key is still an agent (save_page holds it to a consent). Mounted from
main.py with the auth helpers injected, so this module does not import main.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit

#: The legacy member permission strings the knowledge routes already use.
_READ, _MANAGE = "knowledge:read", "knowledge:manage"


class PageBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    title: str
    body: str = ""


class RestoreBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    revision: Any = None


class ProposeBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    title: Optional[str] = None
    body: Optional[str] = None
    edits: Optional[list[dict[str, Any]]] = None
    note: str = ""


class MaintainBody(BaseModel):
    project_id: str
    actor: Optional[str] = None


def build_wiki_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
) -> APIRouter:
    from boss_engine.services import change_consent, wiki

    router = APIRouter(tags=["wiki"])

    def _fail(error: Exception):
        if isinstance(error, change_consent.ConsentRequired):
            raise HTTPException(status_code=403, detail=str(error)) from error
        if isinstance(error, wiki.WikiError):
            raise HTTPException(status_code=getattr(error, "status", 422),
                                detail=str(error)) from error
        raise error

    @router.get("/v1/wiki/pages")
    @permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
    def list_pages(project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"project_id": project_id, "pages": wiki.list_pages(connection, project_id)}

    @router.get("/v1/wiki/pages/{slug}")
    @permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
    def get_page(slug: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return wiki.get_page(connection, project_id, slug)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.get("/v1/wiki/pages/{slug}/revisions")
    @permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
    def revisions(slug: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return {"slug": slug,
                        "revisions": wiki.list_revisions(connection, project_id, slug)}
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.put("/v1/wiki/pages/{slug}")
    @permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
    def save(slug: str, body: PageBody) -> dict[str, Any]:
        """A person's edit is written now; an agent principal is refused (it proposes)."""
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            try:
                return wiki.save_page(connection, project_id=body.project_id, actor=actor,
                                      slug=slug, title=body.title, body=body.body)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.post("/v1/wiki/pages/{slug}/restore")
    @permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
    def restore(slug: str, body: RestoreBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            try:
                return wiki.restore(connection, project_id=body.project_id, actor=actor,
                                    slug=slug, revision=body.revision)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.post("/v1/wiki/pages/{slug}/propose")
    @permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
    def propose(slug: str, body: ProposeBody) -> dict[str, Any]:
        """Park a change for a person: nothing is written until they approve this exact diff."""
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            try:
                return wiki.propose_patch(connection, project_id=body.project_id, actor=actor,
                                          slug=slug, title=body.title, body=body.body,
                                          edits=body.edits, note=body.note)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.post("/v1/wiki/maintain")
    @permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
    def maintain(body: MaintainBody) -> dict[str, Any]:
        """Refresh the pages rendered from the knowledge graph (proposed where a person edited)."""
        with begin(engine) as connection:
            require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            return wiki.maintain(connection, body.project_id)

    return router
