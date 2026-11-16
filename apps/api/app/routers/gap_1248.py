"""BOSSAPP-1248: record a builder prompt, and the commit a publish put on the live URL.

The reconciler mounts ``router``. The signed-in principal is the actor. A body cannot
name a different org or a different actor. A publish with no commit is stored as
"no commit"; the route does not invent a SHA.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from boss_engine.db import begin
from boss_engine.identity import P, Scope, authenticated, permit
from boss_engine.services import builder_audit
from boss_engine.services.builder_audit import BuilderAuditError

router = APIRouter(tags=["builder-audit"])


class PromptIn(BaseModel):
    prompt: str
    project_id: Optional[str] = None
    org_id: Optional[str] = None


class PublishIn(BaseModel):
    live_url: str
    commit: Optional[str] = None
    project_id: Optional[str] = None
    org_id: Optional[str] = None


def _actor(request: Request) -> Any:
    """The guard's verified context. Never a field from the body."""
    return getattr(request.state, "identity", None)


def _org(actor: Any, body_org: Optional[str]) -> str:
    principal = getattr(actor, "principal", None)
    if actor is None or principal is None:
        raise BuilderAuditError("Sign in to record this.", status=401)
    org = str(principal.org_id)
    if body_org and str(body_org) != org:
        raise BuilderAuditError(
            "The org on the request does not match the signed-in org.", status=403)
    return org


def _answer(work: Any) -> Any:
    try:
        return work()
    except BuilderAuditError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/v1/gap/1248/builder-prompts")
@authenticated()
def post_builder_prompt(body: PromptIn, request: Request) -> dict[str, Any]:
    """One audit row for this builder prompt."""
    def work() -> dict[str, Any]:
        actor = _actor(request)
        org_id = _org(actor, body.org_id)
        with begin() as connection:
            recorded = builder_audit.record_builder_prompt(
                connection, org_id=org_id, prompt=body.prompt, actor=actor,
                project_id=body.project_id)
            return recorded.as_dict()

    return _answer(work)


@router.post("/v1/gap/1248/publishes")
@authenticated()
def post_publish(body: PublishIn, request: Request) -> dict[str, Any]:
    """One audit row for the commit this publish put on the live URL."""
    def work() -> dict[str, Any]:
        actor = _actor(request)
        org_id = _org(actor, body.org_id)
        with begin() as connection:
            recorded = builder_audit.record_publish(
                connection, org_id=org_id, live_url=body.live_url, commit=body.commit,
                actor=actor, project_id=body.project_id)
            return recorded.as_dict()

    return _answer(work)


@router.get("/v1/gap/1248/audit")
@permit(P.AUDIT_READ, scope=Scope.org())
def list_builder_audit(request: Request) -> dict[str, Any]:
    """Builder-prompt and publish rows for the signed-in org."""
    def work() -> dict[str, Any]:
        actor = _actor(request)
        org_id = _org(actor, None)
        with begin() as connection:
            return {"rows": builder_audit.rows_for(connection, org_id)}

    return _answer(work)
