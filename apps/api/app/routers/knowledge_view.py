"""The Graph area's read routes (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §3; BOSSAPP-768).

GET /v1/knowledge/view/overview        whole repo: area bubbles, Rules and Learning bubbles, edges
GET /v1/knowledge/view/neighbourhood   focus: a node and its neighbours within 1-3 hops
GET /v1/knowledge/view/node/{id}       the right panel (connections paged 20 at a time)
GET /v1/knowledge/view/tree/children   the lazy project / rules / learning tree
GET /v1/knowledge/view/search          type-ahead by name (meaning=1: the hybrid search)

Payloads are packages/api-client's "KNOWLEDGE GRAPH VIEW" types exactly; the logic lives in
engine/boss_engine/services/knowledge/view.py. Every route reads with knowledge:read at the
project; decisions and precedents also need decision:read, and are absent (never counted, never
drawn) for a caller without it. The rules review and live routes are sibling routers.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services.knowledge.learning_conversations import mend_names
from boss_engine.services.knowledge import view
from boss_engine.services.knowledge.service import READ

router = APIRouter(prefix="/v1/knowledge/view", tags=["knowledge"])
_settings = get_settings()
_engine = get_engine(_settings)


def _actor(connection, project_id: str, actor: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, READ)


def _holds(request: Request, connection, project_id: str, actor: str, permission: str) -> bool:
    """Does the caller hold ``permission`` at the project? Asked without recording a denial: a
    missing one only withholds rows, it refuses nothing."""
    from apps.api.app.routers.identity_access import request_context
    from boss_engine.identity import authz
    from boss_engine.identity.scope import ResolvedScope

    try:
        ctx = request_context(request)
    except Exception:  # noqa: BLE001 - no identity on this request: the member's permissions decide
        ctx = None
    if ctx is not None:
        held = authz.effective_permissions(
            ctx, ResolvedScope.in_project(ctx.principal.org_id, project_id)).permissions
        return permission in held
    from apps.api.app.main import members
    from boss_engine.services.members import ProjectMemberNotFoundError

    try:
        member = members.get_member(connection, project_id, actor)
    except ProjectMemberNotFoundError:
        try:
            member = members._get_org_member_for_project(connection, project_id, actor)
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001
        return False
    return permission in (member.get("permissions") or [])


def _withheld(request: Request, connection, project_id: str, actor: str) -> frozenset[str]:
    needed: dict[str, list[str]] = {}
    for kind, permission in view.KIND_PERMISSIONS.items():
        needed.setdefault(permission, []).append(kind)
    out: set[str] = set()
    for permission, kinds in needed.items():
        if not _holds(request, connection, project_id, actor, permission):
            out.update(kinds)
    return frozenset(out)


def _http(error: Exception) -> HTTPException:
    if isinstance(error, HTTPException):
        return error
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ValueError):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(status_code=500, detail=error.__class__.__name__)


def _read(request: Request, project_id: str, actor: str, read):
    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor)
            # A conversation still named by its thread id is named on read (mend_names).
            return mend_names(connection, read(connection, _withheld(request, connection, project_id, actor)))
    except Exception as error:  # noqa: BLE001 — mapped to HTTP
        raise _http(error) from error


@router.get("/overview")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def overview(
    request: Request,
    project_id: str = Query(...),
    actor: str = Query(...),
    layers: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    area: Optional[str] = Query(None),
) -> dict[str, Any]:
    return _read(request, project_id, actor, lambda c, withheld: view.overview(
        c, project_id, layers=view.parse_layers(layers), repo=repo or None, area=area or None,
        withheld=withheld))


@router.get("/neighbourhood")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def neighbourhood(
    request: Request,
    project_id: str = Query(...),
    actor: str = Query(...),
    node: str = Query(...),
    depth: int = Query(1),
    layers: Optional[str] = Query(None),
    limit: int = Query(80),
) -> dict[str, Any]:
    return _read(request, project_id, actor, lambda c, withheld: view.neighbourhood(
        c, project_id, node, depth=depth, layers=view.parse_layers(layers), limit=limit,
        withheld=withheld))


@router.get("/node/{node_id:path}")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def node(
    request: Request,
    node_id: str,
    project_id: str = Query(...),
    actor: str = Query(...),
    edge: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    offset: int = Query(0),
) -> dict[str, Any]:
    return _read(request, project_id, actor, lambda c, withheld: view.node_detail(
        c, project_id, node_id, edge=edge or None, direction=direction or None, offset=offset,
        withheld=withheld, settings=_settings))


@router.get("/tree/children")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def tree_children(
    request: Request,
    project_id: str = Query(...),
    actor: str = Query(...),
    parent: Optional[str] = Query(None),
    tab: str = Query("project"),
    q: Optional[str] = Query(None),
    offset: int = Query(0),
) -> dict[str, Any]:
    return _read(request, project_id, actor, lambda c, withheld: view.tree_children(
        c, project_id, tab=tab, parent=parent or None, q=q, offset=offset, withheld=withheld))


@router.get("/search")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def search(
    request: Request,
    project_id: str = Query(...),
    actor: str = Query(...),
    q: str = Query(...),
    layers: Optional[str] = Query(None),
    limit: int = Query(20),
    meaning: int = Query(0),
) -> dict[str, Any]:
    return _read(request, project_id, actor, lambda c, withheld: view.search(
        c, project_id, q, layers=view.parse_layers(layers), limit=limit, meaning=bool(meaning),
        withheld=withheld, settings=_settings))
