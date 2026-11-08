"""WHERE PEOPLE DECIDE: the compliance list's HTTP surface (Govern › Where people decide;
services/decide_list.py is the one source of the list; docs/product/wireframes/agentic-factory/
1-where-people-decide.html).

GET  /v1/decide              the 7 + 1 cards (rule, who decides, scope, 24 h count, trail),
                             the people a card may name, and the one-line activity summary
GET  /v1/decide/activity     the one line, plus what each area agent did alone vs. waited for a
                             person in the last 24 h, hour by hour (Agents at work)
POST /v1/decide/understand   "Change a rule in plain words": Helm's structured turn → a Rule
                             change card (edits, what is already allowed, the 30-day replay)
POST /v1/decide/replay       the 30-day replay of edits the person changed by hand
PUT  /v1/decide/rules/{kind} one card's Save (stricter only; 422 when looser)
POST /v1/decide/apply        Apply N changes from a Rule change card (all or nothing)

WHO MAY DO WHAT. Reading the list is decision:read. Asking Helm to read words is a model turn,
so it needs work_item:manage (never a viewer). CHANGING THE LIST IS ITSELF ON THE LIST: every
write declares approval_rule:manage — Owner only, step-up (the passkey), never an API key.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit

router = APIRouter(tags=["govern"])
_settings = get_settings()
_engine = get_engine(_settings)


def _main():
    from apps.api.app import main

    return main


def _org(connection, project_id: str) -> str:
    from boss_engine.multirepo.common import org_of_project

    return str(org_of_project(connection, project_id) or _settings.default_org_id)


def _viewer(request: Request) -> Optional[str]:
    from apps.api.app import identity_guard

    ctx = identity_guard.request_auth(request)
    return ctx.principal.id if ctx is not None else None


def _write_actor(connection, project_id: str, claimed: Optional[str]) -> str:
    """The enforced principal; under legacy auth, a member who may change approval rules."""
    from apps.api.app import identity_guard

    enforced = identity_guard.request_actor()
    if enforced:
        return enforced
    return _main().require_project_write_actor(connection, project_id, claimed, "role_review")


def _refused(fn):
    from boss_engine.services.decide_list import RuleLooser

    try:
        return fn()
    except RuleLooser as refused:
        raise HTTPException(status_code=422, detail={"error": "stricter_only",
                                                     "message": str(refused)}) from refused


@router.get("/v1/decide")
@permit(P.DECISION_READ, scope=Scope.project(query="project_id"))
def get_decide(request: Request, project_id: str) -> dict[str, Any]:
    from boss_engine.services import decide_list

    main = _main()
    with begin(_engine) as connection:
        main.require_project_reader(connection, project_id)
        board = decide_list.board(connection, project_id=project_id,
                                  org_id=_org(connection, project_id), viewer=_viewer(request))
        board["activity"] = decide_list.activity(connection, project_id=None)["summary"]
    return board


@router.get("/v1/decide/activity")
@permit(P.AUTONOMY_READ, scope=Scope.project(query="project_id"))
def get_decide_activity(project_id: str) -> dict[str, Any]:
    from boss_engine.services import decide_list

    with begin(_engine) as connection:
        _main().require_project_reader(connection, project_id)
        return decide_list.activity(connection, project_id=project_id)


class UnderstandBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str = Field(min_length=1, max_length=200)
    words: str = Field(..., max_length=4000)
    tz: str = Field(default="UTC", max_length=64)
    actor: Optional[str] = None


@router.post("/v1/decide/understand")
@permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
def decide_understand(payload: UnderstandBody, request: Request) -> dict[str, Any]:
    """Helm turns the words into structured edits (the one understanding service's turn, subject
    `rule_change`), each checked stricter-only and replayed against 30 days. Writes nothing."""
    from boss_engine.services import decide_list

    from .project_experience import _actor

    with begin(_engine) as connection:
        _main().require_project_reader(connection, payload.project_id)
        return decide_list.understand_rule_change(
            connection, _settings, payload.project_id, actor=_actor(request),
            words=payload.words, tz=payload.tz, org_id=_org(connection, payload.project_id))


class ReplayBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str = Field(min_length=1, max_length=200)
    edits: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


@router.post("/v1/decide/replay")
@permit(P.DECISION_READ, scope=Scope.project(body="project_id"), readonly_post=True)
def decide_replay(payload: ReplayBody) -> dict[str, Any]:
    """The 30-day replay for edits changed by hand on the Rule change card (read-only)."""
    from boss_engine.services import decide_list

    with begin(_engine) as connection:
        _main().require_project_reader(connection, payload.project_id)
        return {"replay": decide_list.replay(connection, payload.edits)}


class RuleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    #: 'install', or the project id this rule is for (a project only adds to the install's).
    scope: str = Field(default="install", min_length=1, max_length=200)
    rule: dict[str, Any]
    actor: Optional[str] = None


@router.put("/v1/decide/rules/{kind}")
@permit(P.APPROVAL_RULE_MANAGE, scope=Scope.org())
def put_decide_rule(kind: str, payload: RuleBody) -> dict[str, Any]:
    """One card's Save · passkey. Stricter only: anything looser than the backstop is 422."""
    from boss_engine.services import decide_list

    with begin(_engine) as connection:
        actor = _write_actor(connection, payload.project_id, payload.actor)
        if payload.scope != decide_list.INSTALL:
            exists = connection.execute(text(
                "SELECT 1 FROM projects WHERE id = :p"), {"p": payload.scope}).scalar()
            if not exists:
                raise HTTPException(status_code=404, detail="no such project")
        saved = _refused(lambda: decide_list.save(
            connection, kind, payload.rule, actor=actor, scope=payload.scope, via="card",
            project_id=payload.project_id))
        return {"saved": saved, "line": decide_list.describe_rule(kind, saved["rule"])}


class ApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    edits: list[dict[str, Any]] = Field(min_length=1, max_length=20)
    words: str = Field(default="", max_length=4000)
    actor: Optional[str] = None


@router.post("/v1/decide/apply")
@permit(P.APPROVAL_RULE_MANAGE, scope=Scope.org())
def decide_apply(payload: ApplyBody) -> dict[str, Any]:
    """Apply N changes. Changing the list is on the list, so this asks for the passkey (the
    permission's step-up); every edit is validated before any is written."""
    from boss_engine.services import decide_list

    with begin(_engine) as connection:
        actor = _write_actor(connection, payload.project_id, payload.actor)
        applied = _refused(lambda: decide_list.apply_edits(
            connection, payload.edits, actor=actor, words=payload.words,
            project_id=payload.project_id))
        return {"applied": applied}
