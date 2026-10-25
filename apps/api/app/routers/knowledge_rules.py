"""Rules review: the business rules agents propose, reviewed by a person (BOSSAPP-768).

GET  /v1/knowledge/view/rules                    the queue: rules by status, scan and area
POST /v1/knowledge/view/rules/{rule_id}/approve   approved: trusted input for every agent
POST /v1/knowledge/view/rules/{rule_id}/edit      version n+1, approved in the same act
POST /v1/knowledge/view/rules/{rule_id}/reject    closed, with the reviewer's note

A sibling of routers/knowledge_view.py (the Graph area's read path), so the two build apart.
Wire shapes: packages/api-client "KNOWLEDGE GRAPH VIEW" (KgRulesResponse, KgRuleActionRequest,
KgRuleActionResponse). The review actions need knowledge:review (owners and admins; never
delegable, so no agent run holds it) and step_up=confirmed in the body; the reviewer recorded on
the rule, its approved_by edge and kg_audit.actor_ref is the request's verified actor. An agent's
credential is refused here whatever it holds: agents propose rules, people approve them.
"""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services.knowledge import rules
from boss_engine.services.knowledge.service import READ

router = APIRouter(prefix="/v1/knowledge/view", tags=["knowledge"])
_settings = get_settings()
_engine = get_engine(_settings)
REVIEW = "knowledge:review"
LOG = logging.getLogger(__name__)


def _actor(connection, project_id: str, actor: str, permission: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, permission)


def _actor_kind() -> str:
    """person, unless the request is an agent's (a run token or an agent principal)."""
    try:
        from apps.api.app.identity_guard import REQUEST_IDENTITY
        from boss_engine.identity.principal import PrincipalKind
    except ImportError:  # pragma: no cover — the guard ships with the API
        return "person"
    ctx = REQUEST_IDENTITY.get()
    if ctx is None:
        return "person"
    if getattr(ctx, "run_token", None) is not None or ctx.principal.kind == PrincipalKind.AGENT:
        return "agent"
    return "person"


def _reviewer(claimed: str) -> tuple[str, Optional[str]]:
    """WHO REVIEWED, AS A PERSON: (stable principal id, display name) of the signed-in person —
    the same test an approval uses (identity.approvals._approver: a human session, never a
    bridge or legacy principal). Approvals stored the body's legacy member id ("ceo" for an
    Owner), so 11 approved rules in northpeak-bank named a role key, not the operator who
    approved them (brec_77cc82cb106640b09d44). Without a verified person session (the legacy
    console path, tests) the claimed actor stands, with no name — the console renders such a
    legacy record as "an owner (legacy record)", never the bare id."""
    try:
        from apps.api.app.identity_guard import REQUEST_IDENTITY
        from boss_engine.identity.legacy import is_bridge_principal
    except ImportError:  # pragma: no cover — the guard ships with the API
        return claimed, None
    ctx = REQUEST_IDENTITY.get()
    if ctx is not None and ctx.is_human_session and not is_bridge_principal(ctx.principal.id):
        name = str(getattr(ctx.principal, "display_name", "") or "").strip() or None
        return ctx.principal.id, name
    return claimed, None


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


@router.get("/rules")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def list_rules(
    project_id: str = Query(...),
    actor: str = Query(...),
    status: Optional[Literal["proposed", "approved", "rejected", "superseded"]] = Query(None),
    scan_id: Optional[str] = Query(None),
    area: Optional[str] = Query(None),
) -> dict[str, Any]:
    """The Rules review queue (KgRulesResponse): each rule with its evidence (symbol, path,
    lines, excerpt), provenance and review, newest first, and the counts per status."""
    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return rules.list_rules(connection, project_id=project_id, status=status,
                                    scan_id=scan_id, area=area)
    except Exception as error:  # noqa: BLE001 — mapped to HTTP below
        raise _http(error) from error


class RuleScopeBody(BaseModel):
    repo: Optional[str] = None
    areas: Optional[list[str]] = None
    packages: Optional[list[str]] = None
    symbols: Optional[list[str]] = None


class RuleActionBody(BaseModel):
    project_id: str
    actor: str
    step_up: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=4000)
    # Edit only: the new version's fields.
    statement: Optional[str] = None
    title: Optional[str] = None
    category: Optional[Literal["validation", "calculation", "limit", "state_transition",
                               "authorization", "other"]] = None
    scope: Optional[RuleScopeBody] = None


def _review(body: RuleActionBody, action, **kwargs) -> dict[str, Any]:
    if body.step_up != "confirmed":
        raise HTTPException(status_code=403, detail="knowledge:review requires step_up=confirmed")
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, REVIEW)
            reviewer, reviewer_name = _reviewer(body.actor)
            done = action(connection, project_id=body.project_id, actor_ref=reviewer,
                          actor_name=reviewer_name, actor_kind=_actor_kind(), note=body.note,
                          **kwargs)
            _settle_scan_gate(connection, body.project_id, done)
            return done
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


def _settle_scan_gate(connection, project_id: str, done: dict[str, Any]) -> None:
    """The review that empties a business_rules_scan's queue closes that run's review gate in the
    same act (rule_scan.settle_review_gate), not on the runner's next tick. A gate that cannot be
    settled now never fails the review: the runner settles it on its tick."""
    scan_id = next((str(part.get("scan_id")) for part in (done.get("rule"), done.get("previous"))
                    if isinstance(part, dict) and part.get("scan_id")), "")
    if not scan_id:
        return
    from boss_engine.services.knowledge import rule_scan

    try:
        with connection.begin_nested():
            rule_scan.settle_review_gate(connection, _settings, project_id, scan_id)
    except Exception:  # noqa: BLE001 — settled by the runner's tick instead
        LOG.warning("rules review: the gate of scan %s was left for the runner", scan_id, exc_info=True)


@router.post("/rules/{rule_id}/approve")
@permit(P.KNOWLEDGE_REVIEW, scope=Scope.project(body="project_id"))
def approve(rule_id: str, body: RuleActionBody) -> dict[str, Any]:
    return _review(body, rules.approve_rule, rule_id=rule_id)


@router.post("/rules/{rule_id}/edit")
@permit(P.KNOWLEDGE_REVIEW, scope=Scope.project(body="project_id"))
def edit(rule_id: str, body: RuleActionBody) -> dict[str, Any]:
    scope = body.scope.model_dump(exclude_none=True) if body.scope is not None else None
    return _review(body, rules.edit_rule, rule_id=rule_id, statement=body.statement, title=body.title,
                   category=body.category, scope=scope)


@router.post("/rules/{rule_id}/reject")
@permit(P.KNOWLEDGE_REVIEW, scope=Scope.project(body="project_id"))
def reject(rule_id: str, body: RuleActionBody) -> dict[str, Any]:
    return _review(body, rules.reject_rule, rule_id=rule_id)
