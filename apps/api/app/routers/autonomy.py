"""DEV AUTONOMY's HTTP surface (BOSSAPP-1441). The console screens come later, after the
operator approves the wireframe; this is the engine and API core.

GET  /v1/autonomy/policy                 the policy in force for a project, its org default, and
                                         the one line Setup and Govern show
PUT  /v1/autonomy/policy                 a project's own row — stricter than the org only (422)
PUT  /v1/autonomy/org-policy             the org default
GET  /v1/autonomy/resolve                "is this production?" for a target, a merge, an
                                         environment or BOSS itself (services/production.py)
PUT  /v1/deployment-targets/{id}/tier    a target's tier: dev | staging | prod (or null)
PUT  /v1/repository-bindings/{id}/ships-to   where a repository's default branch ships
GET  /v1/autonomy/decided                "Decided for you": every auto-decision, latest first
POST /v1/autonomy/decided/{id}/undo      undo it by its stored undo kind
POST /v1/autonomy/decided/{id}/reviewed  "Looks good"
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Production, Scope, permit

router = APIRouter(tags=["autonomy"])
_settings = get_settings()
_engine = get_engine(_settings)


def _main():
    from apps.api.app import main

    return main


def _org(connection, project_id: str) -> str:
    from boss_engine.multirepo.common import org_of_project

    return str(org_of_project(connection, project_id) or _settings.default_org_id)


def _write_actor(connection, project_id: str, claimed: Optional[str]) -> str:
    """The enforced principal; under legacy auth, a member who may change autonomy."""
    from apps.api.app import identity_guard

    enforced = identity_guard.request_actor()
    if enforced:
        return enforced
    return _main().require_autonomy_operator(connection, project_id, claimed)


def _refused(fn):
    from boss_engine.services.autonomy_policy import PolicyLooser

    try:
        return fn()
    except PolicyLooser as refused:
        raise HTTPException(status_code=422, detail={"error": "stricter_only",
                                                     "message": str(refused)}) from refused
    except ValueError as refused:
        raise HTTPException(status_code=422, detail={"error": "invalid",
                                                     "message": str(refused)}) from refused


@router.get("/v1/autonomy/policy")
@permit(P.AUTONOMY_READ, scope=Scope.project(query="project_id"))
def get_autonomy_policy(project_id: str) -> dict[str, Any]:
    from boss_engine.services import autonomy_policy as ap
    from boss_engine.services.production import resolve_install

    with begin(_engine) as connection:
        _main().require_project_reader(connection, project_id)
        org_id = _org(connection, project_id)
        return {"policy": ap.describe(ap.load(connection, project_id, org_id=org_id)),
                "org_default": ap.describe(ap.load_org(connection, org_id)),
                "install": resolve_install().as_dict()}


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    policy: dict[str, Any]
    actor: Optional[str] = None


@router.put("/v1/autonomy/policy")
@permit(P.AUTONOMY_MANAGE, scope=Scope.project(body="project_id"))
def put_autonomy_policy(payload: PolicyBody) -> dict[str, Any]:
    """A project's own policy: stricter than its org's only (422 when looser)."""
    from boss_engine.services import autonomy_policy as ap

    with begin(_engine) as connection:
        actor = _write_actor(connection, payload.project_id, payload.actor)
        saved = _refused(lambda: ap.save(connection, kind=ap.PROJECT,
                                         scope_id=payload.project_id, raw=payload.policy,
                                         actor=actor))
        return {"policy": ap.describe(saved)}


@router.put("/v1/autonomy/org-policy")
@permit(P.ORG_UPDATE, scope=Scope.org())
def put_autonomy_org_policy(payload: PolicyBody, request: Request) -> dict[str, Any]:
    """The org default every project starts from (`project_id` names the org by one of its
    projects, which must be the caller's own org)."""
    from apps.api.app import identity_guard
    from boss_engine.services import autonomy_policy as ap

    with begin(_engine) as connection:
        org_id = _org(connection, payload.project_id)
        ctx = identity_guard.request_auth(request)
        if ctx is not None and str(ctx.principal.org_id) != org_id:
            raise HTTPException(status_code=404, detail="no such project")
        actor = _write_actor(connection, payload.project_id, payload.actor)
        saved = _refused(lambda: ap.save(connection, kind=ap.ORG, scope_id=org_id,
                                         raw=payload.policy, actor=actor,
                                         event_project_id=payload.project_id))
        return {"org_default": ap.describe(saved),
                "policy": ap.describe(ap.load(connection, payload.project_id, org_id=org_id))}


@router.get("/v1/autonomy/resolve")
@permit(P.AUTONOMY_READ, scope=Scope.project(query="project_id"))
def get_autonomy_resolve(project_id: str, target_id: Optional[str] = None,
                         environment: Optional[str] = None, repo: Optional[str] = None,
                         base_branch: Optional[str] = None,
                         install: bool = False) -> dict[str, Any]:
    """Is this production, does BOSS know, and why — the verdict every gate acts on."""
    from boss_engine.services import production

    with begin(_engine) as connection:
        _main().require_project_reader(connection, project_id)
        if target_id:
            owner = connection.execute(text(
                "SELECT project_id FROM deployment_targets WHERE id = :i"),
                {"i": target_id}).scalar()
            if owner != project_id:
                raise HTTPException(status_code=404, detail="no such deployment target")
        verdict = production.resolve(
            connection, {"install": install, "target_id": target_id, "pr_base": base_branch,
                         "repo": repo, "environment": environment}, project_id=project_id)
        return {"verdict": verdict.as_dict(), "words": verdict.words}


class TierBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: Optional[str] = Field(default=None, max_length=20)
    actor: Optional[str] = None


@router.put("/v1/deployment-targets/{target_id}/tier")
# Relabelling a production target is a production change (as PATCH /v1/deployment-targets is):
# deployment:production is held whenever the resolver says the target is production now — by
# name, by tier, or because BOSS cannot tell (an unlabelled remote server): calling one "dev"
# is what would let BOSS ship to it unasked.
@permit(P.DEPLOYMENT_TARGET_MANAGE, scope=Scope.of("deployment_target", path="target_id"),
        production=Production(target="path:target_id", resolved=True))
def put_deployment_target_tier(target_id: str, payload: TierBody) -> dict[str, Any]:
    """Say what a target is: dev, staging or prod. Null clears it (BOSS then reads its name and
    where it runs; an unlabelled remote server is production)."""
    from boss_engine.services import production
    from boss_engine.services.core import emit_event

    tier = (payload.tier or "").strip().lower() or None
    if tier is not None and tier not in production.TIERS:
        raise HTTPException(status_code=422, detail="tier is dev, staging or prod")
    with begin(_engine) as connection:
        row = connection.execute(text(
            "SELECT project_id FROM deployment_targets WHERE id = :i FOR UPDATE"),
            {"i": target_id}).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="no such deployment target")
        project_id = str(row["project_id"])
        from apps.api.app import identity_guard

        actor = identity_guard.request_actor() or _main()._deployment_target_write_actor(
            {"actor": payload.actor})
        _main().require_project_actor(connection, project_id, actor, "providerops_manage")
        connection.execute(text("UPDATE deployment_targets SET tier = :t, updated_at = now() "
                                "WHERE id = :i"), {"t": tier, "i": target_id})
        emit_event(connection, project_id, "deployment_target.tier_set", actor,
                   "deployment_target", target_id, {"tier": tier})
        return {"target_id": target_id, "tier": tier,
                "verdict": production.resolve_target(connection, target_id).as_dict()}


class ShipsToBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: A deployment target id, 'none' (ships nowhere), or null (unknown: merges into it ask).
    ships_to: Optional[str] = Field(default=None, max_length=200)
    actor: Optional[str] = None


@router.put("/v1/repository-bindings/{binding_id}/ships-to")
# Where a default branch ships decides whether its merges are production releases, so saying it
# is a production change: deployment:production, always.
@permit(P.REPOSITORY_MANAGE, scope=Scope.of("github_repository_binding", path="binding_id"),
        production=Production(always=True))
def put_repository_ships_to(binding_id: str, payload: ShipsToBody) -> dict[str, Any]:
    """Where a repository's default branch ships. A merge into it is a production release when
    it ships to a production target, or when nobody said."""
    from boss_engine.services import production
    from boss_engine.services.core import emit_event

    ships = (payload.ships_to or "").strip() or None
    with begin(_engine) as connection:
        row = connection.execute(text(
            "SELECT project_id, full_name, default_branch FROM github_repository_bindings "
            "WHERE id = :i FOR UPDATE"), {"i": binding_id}).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="no such repository")
        project_id = str(row["project_id"])
        if ships and ships != production.SHIPS_NOWHERE:
            owner = connection.execute(text(
                "SELECT project_id FROM deployment_targets WHERE id = :i"), {"i": ships}).scalar()
            if owner != project_id:
                raise HTTPException(status_code=422,
                                    detail="ships_to is one of this project's deployment "
                                           "targets, or 'none'")
        actor = _write_actor(connection, project_id, payload.actor)
        connection.execute(text(
            "UPDATE github_repository_bindings SET default_branch_ships_to = :s, "
            "updated_at = now() WHERE id = :i"), {"s": ships, "i": binding_id})
        emit_event(connection, project_id, "repository.ships_to_set", actor,
                   "github_repository_binding", binding_id, {"ships_to": ships})
        verdict = production.resolve_merge(connection, project_id=project_id,
                                           repo=row["full_name"],
                                           base_branch=row["default_branch"] or "main")
        return {"binding_id": binding_id, "ships_to": ships, "verdict": verdict.as_dict()}


# ---- "Decided for you": what BOSS decided under dev autonomy, with undo -------------------------

@router.get("/v1/autonomy/decided")
@permit(P.DECISION_READ, scope=Scope.project(query="project_id"))
def get_decided_for_you(project_id: str, limit: int = 50) -> dict[str, Any]:
    """Every card BOSS decided for you under dev autonomy, latest first: who decided, why it was
    not production, when, the links, and the undo it offers (or why there is none)."""
    from boss_engine.services import autonomy_gate

    with begin(_engine) as connection:
        _main().require_project_reader(connection, project_id)
        items = autonomy_gate.decided_for_you(connection, project_id, limit=limit)
        return {"items": items, "count": len(items),
                "unreviewed": sum(1 for i in items if not i["reviewed"])}


class DecidedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None


def _decision_actor(connection, project_id: str, claimed: Optional[str]) -> str:
    from apps.api.app import identity_guard

    return identity_guard.request_actor() or _main().require_project_write_actor(
        connection, project_id, claimed, "decision_resolve")


def _owned(connection, decision_id: str, project_id: str) -> None:
    owner = connection.execute(text("SELECT project_id FROM decisions WHERE id = :d"),
                               {"d": decision_id}).scalar()
    if owner != project_id:
        raise HTTPException(status_code=404, detail="no such decision")


@router.post("/v1/autonomy/decided/{decision_id}/undo")
@permit(P.DECISION_APPROVE, scope=Scope.of("decision", path="decision_id"))
def post_undo_decided(decision_id: str, payload: DecidedBody) -> dict[str, Any]:
    """Undo one auto-decision by the undo kind stored when it was decided (revert the merge, roll
    the deploy back, stop the build it started, revoke the grant...). 409 when it has none."""
    from boss_engine.services import autonomy_gate

    with begin(_engine) as connection:
        _owned(connection, decision_id, payload.project_id)
        actor = _decision_actor(connection, payload.project_id, payload.actor)
        try:
            return autonomy_gate.undo(connection, _settings, decision_id,
                                      project_id=payload.project_id, actor=actor)
        except autonomy_gate.UndoRefused as refused:
            raise HTTPException(status_code=409, detail=str(refused)) from refused
        except LookupError as missing:
            raise HTTPException(status_code=404, detail=str(missing)) from missing


@router.post("/v1/autonomy/decided/{decision_id}/reviewed")
@permit(P.DECISION_APPROVE, scope=Scope.of("decision", path="decision_id"))
def post_reviewed_decided(decision_id: str, payload: DecidedBody) -> dict[str, Any]:
    """"Looks good": a person reviewed what BOSS decided. Nothing else changes."""
    from boss_engine.services import autonomy_gate

    with begin(_engine) as connection:
        _owned(connection, decision_id, payload.project_id)
        actor = _decision_actor(connection, payload.project_id, payload.actor)
        try:
            return autonomy_gate.mark_reviewed(connection, decision_id,
                                               project_id=payload.project_id, actor=actor)
        except autonomy_gate.UndoRefused as refused:
            raise HTTPException(status_code=409, detail=str(refused)) from refused
        except LookupError as missing:
            raise HTTPException(status_code=404, detail=str(missing)) from missing
